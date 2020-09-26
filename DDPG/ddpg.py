from models import Actor, Critic
import torch
from torch import tensor, cat
from torch.optim import Adam
import torch.nn.functional as F
from collections import namedtuple
from memory import NaivePrioritizedBuffer
import numpy as np
from tqdm import tqdm
from torch.utils.tensorboard import SummaryWriter
from env import TSCSEnv

class DDPG():
	def __init__(self,
		inSize, actorNHidden, actorHSize, criticNHidden, criticHSize, 
		nActions, actionRange, actorLR, criticLR, criticWD,
		gamma, tau, epsilon, epsDecay, epsEnd,
		memSize, batchSize, numEpisodes, epLen):

		super(DDPG, self).__init__()
		## Actions
		self.nActions = nActions
		self.actionRange = actionRange

		## Networks
		self.actor = Actor(inSize, actorNHidden, actorHSize, nActions, actionRange)
		self.targetActor = Actor(inSize, actorNHidden, actorHSize, nActions, actionRange)
		self.critic = Critic(inSize, criticNHidden, criticHSize, nActions)
		self.targetCritic = Critic(inSize, criticNHidden, criticHSize, nActions)

		## Define the optimizers for both networks
		self.actorOpt = Adam(self.actor.parameters(), lr=actorLR)
		self.criticOpt = Adam(self.critic.parameters(), lr=criticLR, weight_decay=criticWD)

		## Hard update
		self.targetActor.load_state_dict(self.actor.state_dict())
		self.targetCritic.load_state_dict(self.critic.state_dict())

		## Various hyperparameters
		self.gamma = gamma
		self.tau = tau
		self.epsilon = epsilon
		self.epsDecay = epsDecay
		self.epsEnd = epsEnd

		## Transition tuple to store experience
		self.Transition = namedtuple(
			'Transition',
			('s','a','r','s_','done'))

		## Allocate memory for replay buffer and set batch size
		self.memory = NaivePrioritizedBuffer(memSize)
		self.batchSize = batchSize

		self.numEpisodes = numEpisodes
		self.epLen = epLen
		self.saveModels = 1000

	def select_action(self, state):
		with torch.no_grad():
			noise = np.random.normal(0, self.epsilon, self.nActions)
			action = self.targetActor(state) + noise
			action.clamp_(-self.actionRange, self.actionRange)
			# action = self.actionRange * tanh(action) ## Try this instead of clamp
		return action

	def extract_tensors(self, batch):
		batch = self.Transition(*zip(*batch))
		s = cat(batch.s)
		a = cat(batch.a)
		r = cat(batch.r)
		s_ = cat(batch.s_)
		done = cat(batch.done)
		return s, a, r, s_, done

	def soft_update(self, target, source):
		for target_param, param in zip(target.parameters(), source.parameters()):
			target_param.data.copy_(target_param.data * (1.0 - self.tau) + param.data * self.tau)

	def optimize_model(self):
		if self.memory.can_provide_sample(self.batchSize):
			## Get data from memory
			batch, indices, weights = self.memory.sample(self.batchSize)
			s, a, r, s_, done = self.extract_tensors(batch)
			weights = tensor([weights])

			## Compute target
			maxQ = self.targetCritic(s_, self.targetActor(s_).detach())
			target_q = r + (1.0 - done) * self.gamma * maxQ

			## Update the critic network
			self.criticOpt.zero_grad()
			current_q = self.critic(s, a)
			criticLoss = weights @ F.smooth_l1_loss(current_q, target_q.detach(), reduction='none')
			criticLoss.backward()
			self.criticOpt.step()

			## Update the actor network
			self.actorOpt.zero_grad()
			actorLoss = -self.critic(s, self.actor(s)).mean()
			actorLoss.backward()
			self.actorOpt.step()

			## Copy policy weights over to target net
			self.soft_update(self.targetActor, self.actor)
			self.soft_update(self.targetCritic, self.critic)

			## Updating priority of transition by last absolute td error
			td = torch.abs(target_q - current_q).detach()
			self.memory.update_priorities(indices, td + 1e-5)
			return td.mean().item()

	def decay_epsilon(self):
		self.epsilon *= self.epsDecay
		self.epsilon = max(self.epsilon, self.epsEnd)

	def learn(self, env):
		## Create file to store run data in using tensorboard
		writer = SummaryWriter('runs/ddpg-attemptToReproduce-layerNorm')

		for episode in range(self.numEpisodes):

			## Reset environment to starting state
			state, rms = env.reset()
			episode_reward = 0

			## Log initial scattering at beginning of episode
			initial = rms.item()
			lowest = initial

			for t in tqdm(range(self.epLen)):

				## Select action and observe next state, reward
				action = self.select_action(state)
				nextState, rms, reward, done = env.step(action)
				episode_reward += reward

				# Update current lowest scatter
				current = rms.item()
				if current < lowest:
					lowest = current

				## Check if terminal
				if t == EP_LEN - 1:
					done = 1
				else:
					done = 0

				## Cast reward and done as tensors
				reward = tensor([[reward]]).float()
				done = tensor([[done]])

				## Store transition in memory
				e = self.Transition(state, action, reward, nextState, done)
				self.memory.push(e)

				## Preform bellman update
				td = self.optimize_model()

				## Break out of loop if terminal state
				if done == 1:
					break

				state = nextState

			## Print episode statistics to console
			print(
				f'#:{episode}, ' \
				f'I:{round(initial, 2)}, ' \
				f'Lowest:{round(lowest, 2)}, ' \
				f'F:{round(current, 2)}, '\
				f'Score:{round(episode_reward, 2)}, ' \
				f'td:{round(td, 2)}, ' \
				f'Epsilon: {round(self.epsilon, 2)}')

			## Log score and lowest scattering configuration discovered in tensorboard
			writer.add_scalar('train/score', episode_reward, episode)
			writer.add_scalar('train/lowest', lowest, episode)

			## Save models
			if episode % self.saveModels == 0:
				torch.save(self.targetActor.state_dict(), 'actor.pt')
				torch.save(self.targetCritic.state_dict(), 'critic.pt')

			## Reduce exploration
			self.decay_epsilon()


if __name__ == '__main__':
	# ddpg params
	IN_SIZE = 21
	ACTOR_N_HIDDEN = 2
	ACTOR_H_SIZE = 128
	CRITIC_N_HIDDEN = 6
	CRITIC_H_SIZE = 128
	N_ACTIONS = 8
	ACTION_RANGE = 0.2
	ACTOR_LR = 1e-4
	CRITIC_LR = 1e-3
	CRITIC_WD = 1e-2 ## How agressively to reduce overfitting
	GAMMA = 0.99 ## How much to value future reward
	TAU = 0.001 ## How much to update target network every step
	EPSILON = 0.75 ## Scale of random noise
	EPS_DECAY = 0.9998 ## How slowly to reduce epsilon
	EPS_END = 0.05 ## Lowest epsilon allowed
	MEM_SIZE = 300_000 ## How many samples in priority queue
	MEM_ALPHA = 0.7 ## How much to use priority queue (0 = not at all, 1 = maximum)
	MEM_BETA = 0.5 ## No clue ????
	BATCH_SIZE = 64
	NUM_EPISODES = 30_000
	EP_LEN = 100

	agent = DDPG(
		IN_SIZE, ACTOR_N_HIDDEN, ACTOR_H_SIZE,
		CRITIC_N_HIDDEN, CRITIC_H_SIZE, N_ACTIONS, 
		ACTION_RANGE, ACTOR_LR, CRITIC_LR, CRITIC_WD, 
		GAMMA, TAU, EPSILON, EPS_DECAY, EPS_END, MEM_SIZE, 
		BATCH_SIZE, NUM_EPISODES,EP_LEN)

	agent.memory.alpha = MEM_ALPHA
	agent.memory.beta = MEM_BETA

	## Create env and agent
	env = TSCSEnv()

	## Run training session
	agent.learn(env)