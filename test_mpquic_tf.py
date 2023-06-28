import park
from park.utils.colorful_print import print_red
from park import logger


import random
import collections
import time
import numpy as np
import os

import tensorflow as tf
from tf_agents.agents.dqn import dqn_agent
from tf_agents.eval import metric_utils
from tf_agents.metrics import tf_metrics
from tf_agents.networks import sequential
from tf_agents.policies import py_tf_eager_policy
from tf_agents.policies import random_tf_policy
from tf_agents.replay_buffers import reverb_replay_buffer
from tf_agents.replay_buffers import reverb_utils
from tf_agents.trajectories import (trajectory, restart,transition,termination)
from tf_agents.specs import (tensor_spec, BoundedTensorSpec)
from tf_agents.utils import common
from tf_agents.replay_buffers import tf_uniform_replay_buffer
from tf_agents.trajectories import (time_step_spec, from_transition)
from tf_agents.train.utils import train_utils
from tf_agents.train import (triggers, learner)
from tf_agents.metrics import py_metrics
from tf_agents.networks import q_network


def create_agent(time_step_spec, action_spec, lr=1e-3, epsilon=0.1, tau=0.05, gamma=0.98):

    q_net = q_network.QNetwork(time_step_spec.observation, action_spec, fc_layer_params=[128,64])
    optimizer = tf.optimizers.Adam(learning_rate=lr)
    global_step = tf.compat.v1.train.get_or_create_global_step()
    agent = dqn_agent.DqnAgent(
        time_step_spec,
        action_spec,
        q_network=q_net,
        epsilon_greedy=epsilon,
        gamma=gamma,
        target_update_tau=tau,
        target_update_period=1,
        optimizer= optimizer,
        td_errors_loss_fn=common.element_wise_squared_loss,
        train_step_counter=global_step)

    agent.initialize()
   
    return agent

def create_replay_buffer(collect_data_spec, batch_size=1, buffer_size=100_000):   
    replay_buffer = tf_uniform_replay_buffer.TFUniformReplayBuffer(
        data_spec=collect_data_spec,
        batch_size=batch_size,
        max_length=buffer_size) 
    
    return replay_buffer


class RLAgent(object):
    def __init__(self, state_space, action_space, *args, **kwargs):        
        self.state_space = state_space
        _action_spec = BoundedTensorSpec(shape=action_space.shape, dtype=action_space.dtype, name="action", minimum=0, maximum=action_space.n-1)
        _state_spec = BoundedTensorSpec(shape=state_space.shape, dtype=state_space.dtype, name='observation', minimum=state_space.low, maximum=state_space.high)
        _time_step_spec = time_step_spec(_state_spec)
        
        self.agent = create_agent(_time_step_spec, _action_spec)
        self.buffer = create_replay_buffer(self.agent.collect_data_spec, batch_size=1)
        self.dataset = self.buffer.as_dataset(sample_batch_size=32,num_steps=2)
        self.iterator = iter(self.dataset)

        checkpoint_dir = os.path.join("/tmp/", "mpquic-ckp")
        metrics_dir = os.path.join("/tmp/", "mpquic-metrics")
        train_summary_writer = tf.summary.create_file_writer(
            metrics_dir, flush_millis=1_000)
        train_summary_writer.set_as_default()

        self.train_ckp = common.Checkpointer(
            ckpt_dir = checkpoint_dir,
            max_to_keep=1,
            agent = self.agent,
            policy = self.agent.policy,
            replay_buffer= self.buffer,
            global_step = tf.compat.v1.train.get_or_create_global_step()
        )
        self.train_ckp.initialize_or_restore()

        self.train_metrics = [
            tf_metrics.NumberOfEpisodes(),
            tf_metrics.EnvironmentSteps(),
            tf_metrics.AverageReturnMetric(),
            tf_metrics.ChosenActionHistogram(dtype=np.int64),
        ]

        self.observers = self.train_metrics
        
        self.last_time_step = restart(tf.zeros((1,4), dtype=state_space.dtype), batch_size=1)
        self.last_action_step = self.agent.collect_policy.action(self.last_time_step)        

    def get_action(self, obs, reward, done, prev_info):

        if done:
            next_time_step = termination(observation=tf.convert_to_tensor([obs], dtype=self.state_space.dtype), reward=tf.convert_to_tensor([reward], dtype=np.float32))
        else:
            next_time_step = transition(observation=tf.convert_to_tensor([obs], dtype=self.state_space.dtype), reward=tf.convert_to_tensor([reward], dtype=np.float32))

        traj = from_transition(self.last_time_step, self.last_action_step, next_time_step)        

        self.buffer.add_batch(traj)
        for o in self.observers:
            o(traj)

        actions_histogram = self.train_metrics[3].result()
        # import pdb; pdb.set_trace()
        #actions = {i:actions_histogram.count(i) for i in actions_histogram}
        logger.info(f" episode: {self.train_metrics[0].result()} steps: {self.train_metrics[1].result()} avg return: {self.train_metrics[2].result()}")

        for m in self.train_metrics:
            m.tf_summaries(train_step = self.agent.train_step_counter)

        
        self.last_action_step = self.agent.collect_policy.action(next_time_step)        
        self.last_time_step = next_time_step
        return self.last_action_step.action.numpy()[0].item()
            
    def train_one_iteration(self):
        batch, _ = next(self.iterator)
        train_loss = self.agent.train(batch)
        iteration = self.agent.train_step_counter.numpy()
        episode = self.train_metrics[0].result()

        for m in self.train_metrics:
            m.tf_summaries(train_step = self.agent.train_step_counter)

        logger.info(f" episode: {episode} steps: {self.train_metrics[1].result()} training iteration: {iteration} loss: {train_loss.loss}")
        return iteration, train_loss.loss
        

def main():
    env = park.make('mpquic')
    agent = RLAgent(env.observation_space, env.action_space)
    env.run(agent)

    # gather enough experiences before training starts
    for _ in range(2):        
        env.reset()
        #print(f"Buffer: {agent.buffer.num_frames()}")

    #start training loops after each download / episode
    for _ in range(5):
        env.reset()
        agent.train_one_iteration()

    agent.train_ckp.save(tf.compat.v1.train.get_global_step())
    
if __name__ == "__main__":
    main()