DQN isn't a natural choice for this problem.

DQN outputs one Q-value per discrete action and picks \(\arg\max_a Q(s,a)\). For a 7-dimensional continuous torque vector there is no finite list of actions; the arg max would itself need an optimizer at every step. Gridding each joint into k bins gives \(k^7\) combinations, which grows exponentially (5 bins already gives 78,125).

Better fits:
* **SAC** or **TD3** — off-policy actor–critic with replay; usually the most sample-efficient choice for continuous control.
* **PPO** — on-policy; needs more samples but is simple and robust when the simulator is cheap and parallel.
