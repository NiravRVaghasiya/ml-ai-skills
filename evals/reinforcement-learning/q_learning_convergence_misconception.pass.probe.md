Not quite — "off-policy" is necessary for learning from someone else's data, but it is not sufficient for convergence.

**Tabular case.** Watkins & Dayan's proof assumes every (s, a) is updated infinitely often and the learning rates α_t satisfy Σα_t = ∞, Σα_t² < ∞. A controller that never brakes violates the first condition: there's no data for the brake action at all.

**Function approximation.** With a deep Q-network there's no convergence guarantee — combining nonlinear function approximation, bootstrapping and off-policy updates is Sutton & Barto's "deadly triad". DQN's target network and replay buffer reduce instability, but do not provide a guarantee.

So Q(s, brake) will be whatever the network extrapolates, and because of the max in the target, optimistic errors on that action can propagate. Get some exploratory data that includes braking, or use an offline RL method (e.g. CQL/BCQ-style constraints).
