# Copyright 2019 DeepMind Technologies Limited. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""JAX functions implementing different exploration methods.

This file contains a (growing) list of exploration methods used by RL agents.
Currently, we support noise-bsaed exploration methods, such as adding Gaussian
noise or temporally correlated noise drawn from an OU process.

We also support the computation of intrinsic rewards a la Agent57 / NGU style
exploration (see docstring), which is to be used as part of recurrent cell to
process states and a growing memory of previously visited states.
"""
from typing import Optional

import chex
import jax
import jax.numpy as jnp

from rlax._src import episodic_memory

Array = chex.Array
Scalar = chex.Scalar


def add_gaussian_noise(
    key: Array,
    action: Array,
    stddev: float
) -> Array:
  """Returns continuous action with noise drawn from a Gaussian distribution.

  Args:
    key: a key from `jax.random`.
    action: continuous action scalar or vector.
    stddev: standard deviation of noise distribution.

  Returns:
    noisy action, of the same shape as input action.
  """
  chex.assert_type(action, float)

  noise = jax.random.normal(key, shape=action.shape) * stddev
  return action + noise


def add_ornstein_uhlenbeck_noise(
    key: Array,
    action: Array,
    noise_tm1: Array,
    damping: float,
    stddev: float
) -> Array:
  """Returns continuous action with noise from Ornstein-Uhlenbeck process.

  See "On the theory of Brownian Motion" by Uhlenbeck and Ornstein.
  (https://journals.aps.org/pr/abstract/10.1103/PhysRev.36.823).

  Args:
    key: a key from `jax.random`.
    action: continuous action scalar or vector.
    noise_tm1: noise sampled from OU process in previous timestep.
    damping: parameter for controlling autocorrelation of OU process.
    stddev: standard deviation of noise distribution.

  Returns:
    noisy action, of the same shape as input action.
  """
  chex.assert_rank([action, noise_tm1], 1)
  chex.assert_type([action, noise_tm1], float)

  noise_t = (1. - damping) * noise_tm1 + jax.random.normal(
      key, shape=action.shape) * stddev

  return action + noise_t


def add_dirichlet_noise(
    key: Array,
    prior: Array,
    dirichlet_alpha: float,
    dirichlet_fraction: float
) -> Array:
  """Returns discrete actions with noise drawn from a Dirichlet distribution.

  See "Mastering the Game of Go without Human Knowledge" by Silver et. al. 2017
  (https://discovery.ucl.ac.uk/id/eprint/10045895/1/agz_unformatted_nature.pdf),
  "A General Reinforcement Learning Algorithm that Masters Chess, Shogi and
  Go Through Self-Play" by Silver et. al. 2018
  (http://airesearch.com/wp-content/uploads/2016/01/deepmind-mastering-go.pdf),
  and "Mastering Atari, Go, Chess and  Shogi by Planning with a Learned Model"
  by Schrittwieser et. al., 2019 (https://arxiv.org/abs/1911.08265).

  The AlphaZero family of algorithms adds noise sampled from a symmetric
  Dirichlet distribution to the prior policy generated by MCTS. Because the
  agent then samples from this new, noisy prior over actions, this encourages
  better exploration of the root node's children.

  Specifically, this computes:

          noise ~ Dirichlet(alpha)
          noisy_prior = (1 - fraction) * prior + fraction * noise

  Note that alpha is a single float to draw from a symmetric Dirichlet.

  For reference values, AlphaZero uses 0.3, 0.15, 0.03 for Chess, Shogi, and
  Go respectively, and MuZero uses 0.25 for Atari.


  Args:
    key: a key from `jax.random`.
    prior: 2-dim continuous prior policy vector of shapes [B, N], for B batch
      size and N num_actions.
    dirichlet_alpha: concentration parameter to parametrize Dirichlet
      distribution.
    dirichlet_fraction: float from 0 to 1 interpolating between using only the
      prior policy or just the noise.

  Returns:
    noisy action, of the same shape as input action.
  """
  chex.assert_rank(prior, 2)
  chex.assert_type([dirichlet_alpha, dirichlet_fraction], float)

  batch_size, num_actions = prior.shape
  noise = jax.random.dirichlet(
      key=key,
      alpha=jnp.full(shape=(num_actions,), fill_value=dirichlet_alpha),
      shape=(batch_size,))

  noisy_prior = (1 - dirichlet_fraction) * prior + dirichlet_fraction * noise

  return noisy_prior


@chex.dataclass
class IntrinsicRewardState():
  memory: jnp.ndarray
  next_memory_index: Scalar = 0
  distance_sum: Scalar = 0
  distance_count: Scalar = 0


def episodic_memory_intrinsic_rewards(
    embeddings: Array,
    num_neighbors: int,
    reward_scale: float,
    intrinsic_reward_state: Optional[IntrinsicRewardState] = None,
    constant: float = 1e-3,
    epsilon: float = 1e-4,
    cluster_distance: float = 8e-3,
    max_similarity: float = 8.,
    max_memory_size: int = 30_000):
  """Compute intrinsic rewards for exploration via episodic memory.

  This method is adopted from the intrinsic reward computation used in "Never
  Give Up: Learning Directed Exploration Strategies" by Puigdomènech Badia et
  al., (2020) (https://arxiv.org/abs/2003.13350) and "Agent57: Outperforming the
  Atari Human Benchmark" by Puigdomènech Badia et al., (2020)
  (https://arxiv.org/abs/2002.06038).

  From an embedding, we compute the intra-episode intrinsic reward with respect
  to a pre-existing set of embeddings.

  NOTE: For this function to be jittable, static_argnums=[1,] must be passed, as
  the internal jax.lax.top_k(neg_distances, num_neighbors) computation in
  knn_query cannot be jitted with a dynamic num_neighbors that is passed as an
  argument.

  Args:
    embeddings: Array, shaped [M, D] for number of new state embeddings M and
      feature dim D.
    num_neighbors: int for K neighbors used in kNN query
    reward_scale: The β term used in the Agent57 paper to scale the reward.
    intrinsic_reward_state: An IntrinsicRewardState namedtuple, containing
      memory, next_memory_index, distance_sum, and distance_count.
      NOTE- On (only) the first call to episodic_memory_intrinsic_rewards, the
      intrinsic_reward_state is optional, if None is given, an
      IntrinsicRewardState will be initialized with default parameters,
      specifically, the memory will be initialized to an array of jnp.inf of
      shape [max_memory_size x feature dim D], and default values of 0 will be
      provided for next_memory_index, distance_sum, and distance_count.
    constant: float; small constant used for numerical stability used during
      normalizing distances.
    epsilon: float; small constant used for numerical stability when computing
      kernel output.
    cluster_distance: float; the ξ term used in the Agent57 paper to bound the
      distance rate used in the kernel computation.
    max_similarity: float; max limit of similarity; used to zero rewards when
      similarity between memories is too high to be considered 'useful' for an
      agent.
    max_memory_size: int; the maximum number of memories to store. Note that
      performance will be marginally faster if max_memory_size is an exact
      multiple of M (the number of embeddings to add to memory per call to
      episodic_memory_intrinsic_reward).

  Returns:
    Intrinsic reward for each embedding computed by using similarity measure to
    memories and next IntrinsicRewardState.
  """

  # Initialize IntrinsicRewardState if not provided to default values.
  if not intrinsic_reward_state:
    intrinsic_reward_state = IntrinsicRewardState(
        memory=jnp.inf * jnp.ones(shape=(max_memory_size,
                                         embeddings.shape[-1])))
    # Pad the first num_neighbors entries with zeros.
    padding = jnp.zeros((num_neighbors, embeddings.shape[-1]))
    intrinsic_reward_state.memory = (
        intrinsic_reward_state.memory.at[:num_neighbors, :].set(padding))
  else:
    chex.assert_shape(intrinsic_reward_state.memory,
                      (max_memory_size, embeddings.shape[-1]))

  # Compute the KNN from the embeddings using the square distances from
  # the KNN d²(xₖ, x). Results are not guaranteed to be ordered.
  jit_knn_query = jax.jit(episodic_memory.knn_query, static_argnums=[2,])
  knn_query_result = jit_knn_query(intrinsic_reward_state.memory, embeddings,
                                   num_neighbors)

  # Insert embeddings into memory in a ring buffer fashion.
  memory = intrinsic_reward_state.memory
  start_index = intrinsic_reward_state.next_memory_index % memory.shape[0]
  indices = (jnp.arange(embeddings.shape[0]) + start_index) % memory.shape[0]
  memory = jnp.asarray(memory).at[indices].set(embeddings)

  nn_distances_sq = knn_query_result.neighbor_neg_distances

  # Unpack running distance statistics, and update the running mean dₘ²
  distance_sum = intrinsic_reward_state.distance_sum
  distance_sum += jnp.sum(nn_distances_sq)
  distance_counts = intrinsic_reward_state.distance_count
  distance_counts += nn_distances_sq.size

  # We compute the sum of a kernel similarity with the KNN and set to zero
  # the reward when this similarity exceeds a given value (max_similarity)
  # Compute rate = d(xₖ, x)² / dₘ²
  mean_distance = distance_sum / distance_counts
  distance_rate = nn_distances_sq / (mean_distance + constant)

  # The distance rate becomes 0 if already small: r <- max(r-ξ, 0).
  distance_rate = jnp.maximum(distance_rate - cluster_distance,
                              jnp.zeros_like(distance_rate))

  # Compute the Kernel value K(xₖ, x) = ε/(rate + ε).
  kernel_output = epsilon / (distance_rate + epsilon)

  # Compute the similarity for the embedding x:
  # s = √(Σ_{xₖ ∈ Nₖ} K(xₖ, x)) + c
  similarity = jnp.sqrt(jnp.sum(kernel_output, axis=-1)) + constant

  # Compute the intrinsic reward:
  # r = 1 / s.
  reward_new = jnp.ones_like(embeddings[..., 0]) / similarity

  # Zero the reward if similarity is greater than max_similarity
  # r <- 0 if s > sₘₐₓ otherwise r.
  max_similarity_reached = similarity > max_similarity
  reward = jnp.where(max_similarity_reached, 0, reward_new)

  # r <- β * r
  reward *= reward_scale

  return reward, IntrinsicRewardState(
      memory=memory,
      next_memory_index=start_index + embeddings.shape[0] % max_memory_size,
      distance_sum=distance_sum,
      distance_count=distance_counts)
