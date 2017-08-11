import numpy as np
import tensorflow as tf
import random
import time

from cassiopeia import riotapi
from draftstate import DraftState
import championinfo as cinfo
import matchProcessing as mp
import experienceReplay as er
from rewards import getReward

from copy import deepcopy
import sqlite3
import draftDbOps as dbo

def trainNetwork(online_net, target_net, training_matches, validation_matches, train_epochs, batch_size, buffer_size, load_model = False, verbose = False):
    """
    Args:
        online_net (qNetwork): "live" Q-network to be trained.
        target_net (qNetwork): target Q-network used to generate target values for the online network
        training_matches (list(match)): list of matches to be trained on
        validation_matches (list(match)): list of matches to validate model against
        train_epochs (int): number of times to learn on given data
        batch_size (int): size of each training set sampled from the replay buffer which will be used to update Qnet at a time
        buffer_size (int): size of replay buffer used
        load_model (bool): flag to reload existing model
        verbose (bool): flag for enhanced output
    Returns:
        (loss,validation_accuracy) tuple
    Trains the Q-network Qnet in batches using experience replays.
    """
    num_episodes = len(training_matches)
    if(verbose):
        print("***")
        print("Beginning training..")
        print("  train_epochs: {}".format(train_epochs))
        print("  num_episodes: {}".format(num_episodes))
        print("  batch_size: {}".format(batch_size))
        print("  buffer_size: {}".format(buffer_size))
    tau = 1.e-3 # Hyperparameter used in updating target network
    stash_model = False
    model_stash_interval = 125 # Stashes a copy of the model this often
    # Number of steps to take before doing any training. Needs to be at least batch_size to avoid error when sampling from experience replay
    pre_training_steps = 10*batch_size
    assert(pre_training_steps <= buffer_size), "Replay not large enough for pre-training!"
    # Number of steps to force learner to observe submitted actions
    observations = 2*pre_training_steps
    # Number of steps to take between training
    update_freq = 1 # There are 10 submissions per match
    lr_decay_freq = 10 # Decay learning rate after a set number of epochs
    min_learning_rate = 1.e-6 # Minimum learning rate allowed to decay to
    # We can't validate a winner for submissions generated by the learner,
    # so we will use a winner-less match when getting rewards for such states
    blank_match = {"winner":None}
    loss_over_epochs = []
    epsilon = 1. # Probability of letting the learner submit its own action

    teams = [DraftState.BLUE_TEAM, DraftState.RED_TEAM]
    # Start training
    with tf.Session() as sess:
        if load_model:
            # Open saved model
            online_net.saver.restore(sess,"tmp/model.ckpt")
            print("Checkpoint loaded..")
        else:
            # Otherwise, initialize tensorflow variables
            sess.run(tf.global_variables_initializer())
        # Add target init and update operations to graph
        target_init = create_target_initialization_ops(target_net.name, online_net.name)
        target_update = create_target_update_ops(target_net.name,online_net.name,tau)
        # Initialize target network
        sess.run(target_init)

        for i in range(train_epochs):
            t0 = time.time()
            if((i>0) and (i % lr_decay_freq == 0) and (online_net.learning_rate.eval() >= min_learning_rate)):
                # Decay learning rate accoring to decay schedule
                online_net.learning_rate = 0.50*online_net.learning_rate

            # Initialize experience replay buffer
            experience_replay = er.ExperienceBuffer(buffer_size)

            total_steps = 0
            bad_state_counts = {DraftState.BAN_AND_SUBMISSION:0,
                                DraftState.DUPLICATE_SUBMISSION:0,
                                DraftState.DUPLICATE_ROLE:0,
                                DraftState.INVALID_SUBMISSION:0}
            learner_submitted_counts = 0
            null_action_count = 0
            # Shuffle match presentation order
            shuffled_matches = random.sample(training_matches,len(training_matches))
            for match in shuffled_matches:
                for team in teams:
                    # Process this match into individual experiences
                    experiences = mp.processMatch(match, team)
                    for experience in experiences:
                        # Some experiences include NULL submissions (exclusively bans)
                        # We don't allow the learner to submit NULL picks so skip adding these
                        # to the replay buffer.
                        state,a,rew,_ = experience
                        (cid,pos) = a
                        if cid is None:
                            null_action_count += 1
                            continue
                        experience_replay.store([experience])
                        if(total_steps >= observations):
                            # Let the network predict the next action, if the action leads
                            # to an invalid state add a negatively reinforced experience to the replay buffer.
                            # This helps the network learn the drafting structure.
                            # Ideally we would also let the network predict a random action and evaluate the reward
                            # for the resulting state, but it's not clear how to assign a reward for an action
                            # which was not produced by geniune match data unless it matches the original experience.
                            # At the very least we can look at the networks predicted optimal action and if it
                            # disagrees with what was actually submitted we can adjust its predicted value.
                            pred_act = sess.run(online_net.prediction,feed_dict={online_net.input:[state.formatState()]})
                            pred_act = pred_act[0]
                            (cid,pos) = state.formatAction(pred_act)

                            pred_state = deepcopy(state)
                            pred_state.updateState(cid,pos)

                            state_code = pred_state.evaluateState()
                            if state_code in DraftState.invalid_states:
                                # Prediction moves to illegal state, add negative experience
                                bad_state_counts[state_code] += 1
                                r = getReward(pred_state, blank_match)
                                new_experience = (state, state.formatAction(pred_act), r, pred_state)
                                experience_replay.store([new_experience])
                            elif(state.getAction(*a)!=pred_act and random.random() < epsilon):
                                # Prediction does not move to illegal state, but doesn't match
                                # submission from training example.
                                learner_submitted_counts += 1
                                r = getReward(pred_state, blank_match) # Normally this should be r = 0
                                new_experience = (state, state.formatAction(pred_act), r, pred_state)
                                experience_replay.store([new_experience])

                        if(epsilon > 0.01):
                            # Reduce chance of dampening learner-submitted actions over time
                            epsilon -= 1./(10*len(training_matches)*train_epochs)
                        total_steps += 1

                        # Every update_freq steps we train the network using the replay buffer
                        if (total_steps >= pre_training_steps) and (total_steps % update_freq == 0):
                            training_batch = experience_replay.sample(batch_size)

                            # Calculate target Q values for each example:
                            # For non-temrinal states, targetQ is estimated according to
                            #   targetQ = r + gamma*max_{a} Q_target(s',a).
                            # For terminating states (where state.evaluateState() == DS.DRAFT_COMPLETE) the target is computed as
                            #   targetQ = r
                            updates = []
                            for exp in training_batch:
                                startState,_,reward,endingState = exp
                                if endingState.evaluateState() == DraftState.DRAFT_COMPLETE: # Action moves to terminal state
                                    updates.append(reward)
                                else:
                                    # Each row in predictedQ gives estimated Q(s',a) values for all possible actions for the input state s'.
                                    predictedQ = sess.run(target_net.outQ,
                                                   feed_dict={target_net.input:[endingState.formatState()]})
                                    # To get max_{a} Q(s',a) values take max along *rows* of predictedQ.
                                    maxQ = np.max(predictedQ,axis=1)[0]
                                    updates.append(reward + online_net.discount_factor*maxQ)
                            targetQ = np.array(updates)
                            # Make sure targetQ shape is correct (sometimes np.array likes to return array of shape (batch_size,1))
                            targetQ.shape = (batch_size,)

                            # Update online net using target Q
                            # Experience replay stores action = (champion_id, position) pairs
                            # these need to be converted into the corresponding index of the input vector to the Qnet
                            actions = np.array([startState.getAction(exp[1][0],exp[1][1]) for exp in training_batch])
                            _ = sess.run(online_net.update,
                                     feed_dict={online_net.input:np.stack([exp[0].formatState() for exp in training_batch],axis=0),
                                     online_net.actions:actions,
                                     online_net.target:targetQ})
                            # After the online network has been updated, update target network
                            _ = sess.run(target_update)

            t1 = time.time()-t0
            val_loss,val_acc = validate_model(sess, validation_matches, online_net, target_net)
            loss,train_acc = validate_model(sess, training_matches, online_net, target_net)
            loss_over_epochs.append(loss)
            # Once training is complete, save the updated network
            if(stash_model):
                if(i>0 and i%model_stash_interval==0):
                    # Stash a copy of the current model
                    out_path = online_net.saver.save(sess,"tmp/models/model_E{}.ckpt".format(i))
                    print("Stashed a copy of the current model in {}".format(out_path))
            out_path = online_net.saver.save(sess,"tmp/model_E{}.ckpt".format(train_epochs))
            if(verbose):
                print(" Finished epoch {}/{}: dt {:.2f}, mem {}, loss {:.6f}, train {:.6f}, val {:.6f}".format(i+1,train_epochs,t1,total_steps+null_action_count,loss,train_acc,val_acc),flush=True)
                print("  alpha:{:.4e}".format(online_net.learning_rate.eval()))
                invalid_action_count = sum([bad_state_counts[k] for k in bad_state_counts])
                print("  negative memories added = {}".format(invalid_action_count))
                print("  bad state distributions:")
                for code in bad_state_counts:
                    print("   {} -> {} counts".format(code,bad_state_counts[code]))
                print("  learner submissions: {}".format(learner_submitted_counts))
                print("  model is saved in file: {}".format(out_path))
                print("***",flush=True)

    stats = (loss_over_epochs,train_acc)
    return stats

def create_target_update_ops(target_scope, online_scope, tau=1e-3, name="target_update"):
    """
    Adds operations to graph which are used to update the target network after after a training batch is sent
    through the online network.

    This function should be executed only once before training begins. The resulting operations should
    be run within a tf.Session() once per training batch.

    In double-Q network learning, the online (primary) network is updated using traditional backpropegation techniques
    with target values produced by the target-Q network.
    To improve stability, the target-Q is updated using a linear combination of its current weights
    with the current weights of the online network:
        Q_target = tau*Q_online + (1-tau)*Q_target
    Typical tau values are small (tau ~ 1e-3). For more, see https://arxiv.org/abs/1509.06461.
    Args:
        target_scope (str): name of scope that target network occupies
        online_scope (str): name of scope that online network occupies
        tau (float32): Hyperparameter for combining target-Q and online-Q networks
        name (str): name of operation which updates the target network when run within a session
    Returns: Tensorflow operation which updates the target nework when run.
    """
    target_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=target_scope)
    online_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=online_scope)
    ops = [target_params[i].assign(tf.add(tf.multiply(tau,online_params[i]),tf.multiply(1.-tau,target_params[i]))) for i in range(len(target_params))]
    return tf.group(*ops,name=name)

def create_target_initialization_ops(target_scope, online_scope):
    """
    This adds operations to the graph in order to initialize the target Q network to the same values as the
    online network.

    This function should be executed only once just after the online network has been initialized.

    Args:
        target_scope (str): name of scope that target network occupies
        online_scope (str): name of scope that online network occupies
    Returns:
        Tensorflow operation (named "target_init") which initialize the target nework when run.
    """
    return create_target_update_ops(target_scope, online_scope, tau=1.0, name="target_init")

def validate_model(sess, validation_data, online_net, target_net):
    """
    Validates given model by computing loss and absolute accuracy for validation data using current Qnet estimates.
    Args:
        sess (tensorflow Session): TF Session to run model in
        validation_data (list(dict)): list of matches to validate against
        online_net (qNetwork): "live" Q-network to be validated
        target_net (qNetwork): target Q-network used to generate target values
    Returns:
        stats (tuple(float)): list of statistical measures of performance. stats = (loss,acc)
    """
    val_replay = er.ExperienceBuffer(10*len(validation_data))
    for match in validation_data:
        # Loss is only computed for winning side of drafts
        team = DraftState.RED_TEAM if match["winner"]==1 else DraftState.BLUE_TEAM
        # Process match into individual experiences
        experiences = mp.processMatch(match, team)
        for exp in experiences:
            _,act,_,_ = exp
            (cid,pos) = act
            if cid is None:
                # Skip null actions such as missing/skipped bans
                continue
            val_replay.store([exp])

    n_experiences = val_replay.getBufferSize()
    val_experiences = val_replay.sample(n_experiences)
    state,_,_,_ = val_experiences[0]
    val_states = np.zeros((n_experiences,)+state.formatState().shape)
    val_actions = np.zeros((n_experiences,))
    val_targets = np.zeros((n_experiences,))
    for n in range(n_experiences):
        start,act,rew,finish = val_experiences[n]
        val_states[n,:,:] = start.formatState()
        (cid,pos) = act
        val_actions[n] = start.getAction(cid,pos)
        if finish.evaluateState() == DraftState.DRAFT_COMPLETE:
            # Action moves to terminal state
            val_targets[n] = rew
        else:
            # Each row in predictedQ gives estimated Q(s',a) values for each possible action for the input state s'.
            predicted_Q = sess.run(target_net.outQ,
                            feed_dict={target_net.input:[finish.formatState()]})
            # To get max_{a} Q(s',a) values take max along *rows* of predictedQ.
            max_Q = np.max(predicted_Q,axis=1)[0]
            val_targets[n] = (rew + online_net.discount_factor*max_Q)

    loss,pred_actions = sess.run([online_net.loss, online_net.prediction],
                          feed_dict={online_net.input:val_states,
                                online_net.actions:val_actions,
                                online_net.target:val_targets})
    accurate_predictions = 0.
    for match in validation_data:
        actions = []
        states = []
        blue_score = score_match(sess,online_net,match,DraftState.BLUE_TEAM)
        red_score = score_match(sess,online_net,match,DraftState.RED_TEAM)
        predicted_winner = DraftState.BLUE_TEAM if blue_score >= red_score else DraftState.RED_TEAM
        match_winner = DraftState.RED_TEAM if match["winner"]==1 else DraftState.BLUE_TEAM
        if predicted_winner == match_winner:
            accurate_predictions +=1
    val_accuracy = accurate_predictions/len(validation_data)
    return (loss, val_accuracy)

def score_match(sess, Qnet, match, team):
    """
    Generates an estimated performance score for a team using a specified Qnetwork.
    Args:
        sess (tensorflow Session): TF Session to run model in
        Qnet (qNetwork): tensorflow q network used to score draft
        match (dict): match dictionary with pick and ban data
        team (DraftState.BLUE_TEAM or DraftState.RED_TEAM): team perspective that is being scored
    Returns:
        score (float): estimated value of picks made in the draft submitted by team for this match
    """
    score = 0.
    actions = []
    states = []
    experiences = mp.processMatch(match,team)
    for exp in experiences:
        start,(cid,pos),_,_ = exp
        if cid is None:
            # Ignore missing bans (if present)
            continue
        actions.append(start.getAction(cid,pos))
        states.append(start.formatState())

    # Feed states forward and get scores for submitted actions
    predicted_Q = sess.run(Qnet.outQ,feed_dict={Qnet.input:np.stack(states,axis=0)})
    assert len(actions) == predicted_Q.shape[0], "Number of actions doesn't match number of Q estimates!"
    for i in range(len(actions)):
        score += predicted_Q[i,actions[i]]
    return score
