Probably not - DQN is a poor fit for continuous joint torques.

Why: DQN's target and its action selection both need a max over actions, which means evaluating Q(s, a) for every action in a discrete set. With continuous torques there is no finite set to enumerate, so the max becomes an inner optimization at every step. Discretizing doesn't scale either: 11 torque levels per joint across 7 joints is 11^7, about 19.5 million joint actions, so the output layer explodes combinatorially, and coarse levels lose the fine control a robot arm needs.

Use an algorithm designed for continuous actions:
- SAC (Soft Actor-Critic) or TD3: off-policy actor-critic with a replay buffer, sample-efficient, and common strong baselines for continuous control. DDPG is the older relative of TD3.
- PPO: on-policy, simple and robust, but it needs more environment samples; a good choice when your simulator is fast and can run many parallel environments.

As a rule of thumb, if simulation is expensive per step, start with SAC; if it is cheap and massively parallel, PPO is often easier to tune. Your DQN knowledge transfers: TD targets, replay buffers and target networks all reappear in SAC and TD3.
