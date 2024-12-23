from dataclasses import dataclass, field
from typing import Optional, Tuple, Union

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader

from ddpgportfolio.agent.models import Actor, Critic
from ddpgportfolio.dataset import (
    KrakenDataSet,
)
from ddpgportfolio.memory.memory import (
    Experience,
    PortfolioVectorMemory,
    PrioritizedReplayMemory,
)
from ddpgportfolio.portfolio.portfolio import Portfolio
from utilities.pg_utils import (
    OrnsteinUhlenbeckNoise,
    RewardNormalizer,
    compute_entropy,
    normalize_batch_rewards,
    plot_performance,
)

torch.set_default_device("mps")


@dataclass
class DDPGAgent:
    """Implementation of Deep Deterministic Policy Gradient for the Agent"""

    portfolio: Portfolio
    batch_size: int
    window_size: int
    step_size: int
    n_iter: int
    learning_rate: Optional[float] = 3e-5
    betas: Optional[Tuple[float, float]] = (0.0, 0.9)
    device: Optional[str] = "mps"
    actor: Actor = field(init=False)
    critic: Critic = field(init=False)
    target_actor: nn.Module = field(init=False)
    target_critic: nn.Module = field(init=False)
    actor_optimizer: torch.optim = field(init=False)
    critic_optimizer: torch.optim = field(init=False)
    loss_fn: nn.modules.loss.MSELoss = field(init=False)
    dataloader: DataLoader = field(init=False)
    pvm: PortfolioVectorMemory = field(init=False)
    replay_memory: PrioritizedReplayMemory = field(init=False)
    ou_noise: OrnsteinUhlenbeckNoise = field(init=False)

    gamma: float = 0.9
    tau: float = 0.05
    epsilon: float = 1.0
    epsilon_max: float = 1.0
    epsilon_min: float = 0.01
    epsilon_decay_rate: float = 1e-5
    episode_count: int = 0
    warmup_steps: int = 6000

    def __post_init__(self):
        # create dataset and dataloaders for proper iteration
        kraken_ds = KrakenDataSet(self.portfolio, self.window_size, self.step_size)

        self.dataloader = DataLoader(
            kraken_ds,
            batch_size=self.batch_size,
            pin_memory=True,
            generator=torch.Generator(device=self.device),
        )

        # create actor and critic networks and specify optimizers and learning rates
        m_assets = self.portfolio.m_assets
        m_noncash_assets = self.portfolio.m_noncash_assets
        self.actor = Actor(3, m_noncash_assets)
        self.critic = Critic(3, m_assets)

        self.actor_optimizer = torch.optim.Adam(
            self.actor.parameters(),
            lr=1e-5,
            weight_decay=1e-5,
        )
        self.critic_optimizer = torch.optim.Adam(
            self.critic.parameters(),
            lr=1e-5,
            weight_decay=1e-5,
        )
        self.actor.to(self.device)
        self.critic.to(self.device)

        # loss function for the critic
        self.loss_fn = nn.MSELoss()

        # ou noise initialization
        self.ou_noise = OrnsteinUhlenbeckNoise(size=m_assets, theta=0.20, sigma=0.5)

        # initializing pvm with all cash initially
        self.pvm = PortfolioVectorMemory(self.portfolio.n_samples, m_noncash_assets)
        self.pvm.update_memory_stack(
            torch.zeros(m_noncash_assets), self.window_size - 2
        )
        self.replay_memory = PrioritizedReplayMemory(
            capacity=20000,
        )

    def select_uniform_action(self, m):
        uniform_vec = np.random.uniform(0, 1, size=m)
        return torch.tensor(uniform_vec / np.sum(uniform_vec), dtype=torch.float32)

    def select_action(
        self,
        state: Tuple[torch.tensor, torch.tensor],
        exploration: bool = False,
        action_type: Union[str, str] = "greedy",
    ):
        """Select action using the actor's policy (deterministic action)

        Parameters
        ----------
        state : Tuple[torch.tensor, torch.tensor]
            _description_
        exploration : bool, optional
            _description_, by default False

        Returns
        -------
        _type_
            _description_
        """
        self.actor.eval()

        with torch.no_grad():
            action_logits = self.actor(state)
        if exploration:
            if action_type == "hybrid":
                if self.episode_count < self.warmup_steps:
                    action = self.select_uniform_action(self.portfolio.m_assets)
                    self.episode_count += 1
                    return action[1:]
                else:
                    # transition to ou noise after warm up steps
                    action_type = "ou"
            if action_type == "ou":
                noise = self.ou_noise.sample()
                action_logits += noise
                self.ou_noise.decay_sigma()

            elif action_type == "greedy" and np.random.rand() < self.epsilon:
                action = self.get_uniform_action(self.portfolio.m_assets)
                self.update_epsilon()
                return action[1:]

        action = torch.softmax(action_logits.view(-1), dim=-1)

        # return all non-cash weights
        return action[1:]

    def update_epsilon(self):
        if self.episode_count < self.warmup_steps:
            self.epsilon = (
                self.epsilon_max
                - ((self.epsilon_max - 0.5) / self.warmup_steps) * self.episode_count
            )

        else:
            self.epsilon = max(
                self.epsilon_min,
                self.epsilon
                * np.exp(
                    -self.epsilon_decay_rate * (self.episode_count - self.warmup_steps)
                ),
            )
        self.episode_count += 1

    def update_target_networks(self):
        self.soft_update(self.target_actor, self.actor, self.tau)
        self.soft_update(self.target_critic, self.critic, self.tau)

    def soft_update(
        self, target_network: nn.Module, main_network: nn.Module, tau: float
    ):
        """_summary_

        Parameters
        ----------
        target_network : nn.Module
            _description_
        main_network : nn.Module
            _description_
        tau : float
            _description_
        """
        for target_param, main_param in zip(
            target_network.parameters(), main_network.parameters()
        ):
            target_param.data.copy_(
                tau * main_param.data + (1.0 - tau) * target_param.data
            )

    def train_actor(
        self, experience: Experience, is_weights: torch.tensor, beta: float = 0.05
    ):
        """trains the actor network by maximizing the Q Value from Critic

        Parameters
        ----------
        experience : Experience
            _description_
        is_weights : torch.tensor
            _description_

        Returns
        -------
        _type_
            _description_
        """
        self.actor_optimizer.zero_grad()
        logits = self.actor(experience.state)
        predicted_actions = torch.softmax(logits, dim=1)
        xt, previous_noncash_actions = experience.state
        cash_weight_previous = 1 - previous_noncash_actions.sum(dim=1)
        previous_action = torch.cat(
            [cash_weight_previous.unsqueeze(1), previous_noncash_actions], dim=1
        )
        state = (xt, previous_action)

        # actor has to choose action that maximizes the q value
        # hence we compute the q value and maximize this value
        q_values = self.critic(state, predicted_actions)
        entropy = compute_entropy(predicted_actions)
        actor_loss = -q_values.mean()
        actor_loss = (actor_loss * is_weights).mean()
        # perform backprop

        actor_loss.backward()
        # torch.nn.utils.clip_grad_norm_(self.actor.parameters(), max_norm=1.0)
        self.actor_optimizer.step()
        return predicted_actions[:, 1:], actor_loss.item()

    def _normalize_batch_rewards(self, rewards):
        mean = rewards.mean()
        std = rewards.std()
        return (rewards - mean) / (std + 1e-5)

    def train_critic(self, experience: Experience, is_weights):
        """Train the critic by minimizing loss based on TD Error

        Parameters
        ----------
        experience : Experience
            an object consisting of (st, at, rt, st+1)
            st = (Xt, at-1)
            size of each experience is given by batch_size
        is_weights : bool
            importance sampling weights

        Returns
        -------
        _type_
            _description_
        """
        self.critic_optimizer.zero_grad()
        # critic needs to evaluate good an action is in a state
        # hence we need to add the cash weight back otherwise its biased
        xt, previous_noncash_actions = experience.state
        reward = experience.reward
        cash_weight_previous = 1 - previous_noncash_actions.sum(dim=1)

        # previous action includes cash weight now
        previous_action = torch.cat(
            [cash_weight_previous.unsqueeze(1), previous_noncash_actions], dim=1
        )
        # construct st = (Xt, wt-1)
        state = (xt, previous_action)

        # we need to do the same for action wt at time t
        noncash_actions = experience.action
        cash_weight_action = 1 - noncash_actions.sum(dim=1)
        actions = torch.cat([cash_weight_action.unsqueeze(1), noncash_actions], dim=1)
        predicted_q_values = self.critic(state, actions)

        # calculate target q values using bellman equation
        td_target = reward / self.batch_size

        # compute the critic loss using MSE between predicted Q-values and target Q-values
        # Hence we are minimizing the TD Error
        td_error = td_target - predicted_q_values
        critic_loss = torch.mean(td_error**2)
        critic_loss = (critic_loss * is_weights).mean()

        critic_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.critic.parameters(), max_norm=1.0)
        self.critic_optimizer.step()
        return td_error, critic_loss.item()

    def warm_up_critic(self, n_iterations: int = 100):
        """Warm-up the critic network using the replay buffer before training the actor.

        Parameters
        ----------
        n_iterations : int
            Number of iterations to train the critic during warm-up.
        """
        print(f"Critic warm-up started for {n_iterations} iterations...")
        for _ in range(n_iterations):
            # Sample a batch of experiences from the replay buffer
            if len(self.replay_memory) < self.batch_size:
                print("Not enough samples in the buffer for warm-up. Skipping...")
                return

            experiences, _, is_weights = self.replay_memory.sample(
                batch_size=self.batch_size
            )

            # Perform a critic-only update
            td_error, critic_loss = self.train_critic(experiences, is_weights)

        print("Critic warm-up complete.")

    def pre_train(self):
        """Pretraining the ddpg agent by populating the experience replay buffer"""
        print("pre-training ddpg agent started...")
        print("ReplayMemoryBuffer populating with experience...")
        kraken_ds = KrakenDataSet(self.portfolio, self.window_size, self.step_size)
        reward_normalizer = RewardNormalizer()

        # resetting ou process in the event of a new run
        self.ou_noise.reset()

        n_samples = len(kraken_ds)

        for i in range(1, n_samples + 49):
            xt, prev_index = kraken_ds[i - 1]
            previous_action = self.pvm.get_memory_stack(prev_index)
            state = (xt, previous_action)

            # get current weight from actor network given s = (Xt, wt_prev)
            action = self.select_action(
                state, exploration=True, action_type="hybrid"
            ).detach()

            # store the current action into pvm
            # self.pvm.update_memory_stack(action, prev_index + 1)

            # get the relative price vector from price tensor to calculate reward
            yt = 1 / xt[0, :, -2]
            reward = self.portfolio.get_reward(action, yt, previous_action)
            # reward_normalizer.update(reward.item())
            # normalized_reward = reward_normalizer.normalize(reward.item())
            xt_next, _ = kraken_ds[i]
            next_state = (xt_next, action)
            experience = Experience(
                state, action, reward.item(), next_state, prev_index
            )
            self.replay_memory.add(experience=experience, reward=reward.item())
        print("pretraining done")

        print(f"buffer size: {len(self.replay_memory)}")

        # we subtract one since each experience consists of current state and next state
        assert len(self.replay_memory) == n_samples + 48

        # Critic warm-up phase
        # self.warm_up_critic(n_iterations=200)

    def train(self, n_episodes: int = 50, n_iterations_per_episode: int = 20):
        """Train the agent by training the actor and critic networks

        Parameters
        ----------
        n_episodes : int, optional
            _description_, by default 50
        n_iterations_per_episode : int, optional
            _description_, by default 20

        Raises
        ------
        Exception
            _description_
        """
        if len(self.replay_memory) == 0:
            raise Exception("replay memory is empty.  Please pre-train agent")

        print("Training Started for DDPG Agent")
        # scheduler to perform learning rate decay
        critic_scheduler = torch.optim.lr_scheduler.StepLR(
            self.critic_optimizer, step_size=100, gamma=0.9
        )
        actor_scheduler = torch.optim.lr_scheduler.StepLR(
            self.actor_optimizer, step_size=100, gamma=0.9
        )
        # Training loop
        batch_size = self.batch_size

        critic_losses = []
        actor_losses = []
        rewards = []

        for episode in range(n_episodes):
            # Initialize accumulators for the losses
            episode_actor_loss = 0
            episode_critic_loss = 0
            total_episodic_reward = 0

            # Loop over iterations within the current episode
            for iteration in range(n_iterations_per_episode):
                # Sample a batch of experiences from the replay buffer
                experiences, indices, is_weights = self.replay_memory.sample(
                    batch_size=batch_size
                )

                # get the reward
                reward = experiences.reward
                # Update critic (TD Error)
                td_error, critic_loss = self.train_critic(experiences, is_weights)

                # Update priorities in the replay buffer (for prioritized experience replay)

                self.replay_memory.update_priorities(indices, td_error)

                # Update actor (deterministic policy gradient)
                action, actor_loss = self.train_actor(experiences, is_weights)
                self.pvm.update_memory_stack(
                    action.detach(), experiences.previous_index + 1
                )

                # Accumulate the losses over the iterations for logging
                episode_actor_loss += actor_loss
                episode_critic_loss += critic_loss
                total_episodic_reward += reward.sum().item()

                # Update the learning rate scheduler
                critic_scheduler.step()
                actor_scheduler.step()

            # After finishing the iterations for the episode, log the average losses
            avg_episode_actor_loss = episode_actor_loss / (n_iterations_per_episode)
            avg_episode_critic_loss = episode_critic_loss / (n_iterations_per_episode)
            actor_losses.append(avg_episode_actor_loss)
            critic_losses.append(avg_episode_critic_loss)
            rewards.append(total_episodic_reward / batch_size)

            print(
                f"Episode {episode + 1} - Actor Loss: {avg_episode_actor_loss:.4f}, Critic Loss: {avg_episode_critic_loss:.4f}, Total Reward: {total_episodic_reward/batch_size:.4f}"
            )

            # Update target networks after each episode
            # self.update_target_networks()

        print("Training complete!")
        # performance plots
        plot_performance(actor_losses, critic_losses, rewards)
