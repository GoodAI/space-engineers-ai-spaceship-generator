from http.client import GATEWAY_TIMEOUT
import random
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import logging
import matplotlib.pyplot as plt
import numpy as np
import numpy.typing as npt
from joblib import Parallel, delayed
from pcgsepy.common.jsonifier import json_dumps, json_loads
from pcgsepy.config import (ALIGNMENT_INTERVAL, BIN_POP_SIZE, CS_MAX_AGE,
                            EPSILON_F, MAX_X_SIZE, MAX_Y_SIZE, MAX_Z_SIZE,
                            N_ITERATIONS, N_RETRIES, POP_SIZE, USE_TORCH, X_RANGE, Y_RANGE, Z_RANGE)
from pcgsepy.evo.fitness import (Fitness, box_filling_fitness,
                                 func_blocks_fitness, mame_fitness,
                                 mami_fitness)
from pcgsepy.evo.genops import EvoException
from pcgsepy.fi2pop.utils import create_new_pool, subdivide_solutions
from pcgsepy.hullbuilder import HullBuilder, enforce_symmetry
from pcgsepy.lsystem.constraints import ConstraintLevel
from pcgsepy.lsystem.lsystem import LSystem
from pcgsepy.lsystem.solution import CandidateSolution
from pcgsepy.mapelites.bandit import EpsilonGreedyAgent
from pcgsepy.mapelites.behaviors import BehaviorCharacterization
from pcgsepy.mapelites.bin import MAPBin
from pcgsepy.mapelites.buffer import Buffer, EmptyBufferException
from pcgsepy.mapelites.emitters import (Emitter, HumanPrefMatrixEmitter,
                                        RandomEmitter, emitters,
                                        get_emitter_by_str)
from pcgsepy.nn.estimators import (GaussianEstimator, prepare_dataset)
from tqdm import trange
from typing_extensions import Self


if USE_TORCH:
    from pcgsepy.nn.estimators import MLPEstimator, train_estimator, QuantileEstimator
else:
    class MLPEstimator:
        def __init__(self):
            raise NotImplementedError('This object should never be instantiated')
    
    class QuantileEstimator:
        def __init__(self):
            raise NotImplementedError('This object should never be instantiated')
    
    def train_estimator(estimator, xs, ys, n_epochs):
        raise NotImplementedError('This function should never be called')    


def coverage_reward(mapelites: 'MAPElites') -> float:
    """Compute the coverage reward. Coverage reward is a percentage of new bins over total possible number of bins.

    Args:
        mapelites (MAPElites): The MAP-Elites object.

    Returns:
        float: The coverage reward.
    """
    tot_coverage = mapelites.bins.shape[0] * mapelites.bins.shape[1]
    inc_coverage = sum([1 if any([cs.age == CS_MAX_AGE for cs in map_bin._feasible]) else 0 for map_bin in mapelites.bins.flatten().tolist()])
    return inc_coverage / tot_coverage


def fitness_reward(mapelites: 'MAPElites') -> float:
    """Compute the fitness reward. Fitness reward is the percentage increase of highest current fitness compared to best fitness in the past.

    Args:
        mapelites (MAPElites): The MAP-Elites object.

    Returns:
        float: The fitness reward.
    """
    prev_best, current_best = 0, 0
    for map_bin in mapelites.bins.flatten().tolist():
        for cs in map_bin._feasible:
            if cs.age == CS_MAX_AGE and cs.c_fitness > prev_best:
                current_best = cs.c_fitness
            elif cs.age != CS_MAX_AGE and cs.c_fitness > prev_best:
                prev_best = cs.c_fitness
    fit_diff = current_best - prev_best
    return fit_diff / prev_best


agent_rewards = {
    'coverage_reward': coverage_reward,
    'fitness_reward': fitness_reward
}


class MAPElites:
    def __init__(self,
                 lsystem: LSystem,
                 feasible_fitnesses: List[Fitness],
                 buffer: Buffer,
                 behavior_descriptors: Tuple[BehaviorCharacterization, BehaviorCharacterization],
                 n_bins: Tuple[int, int] = (8, 8),
                 estimator: Optional[Union[GaussianEstimator, MLPEstimator, QuantileEstimator]] = None,
                 emitter: Optional[Emitter] = RandomEmitter(),
                 agent: Optional[EpsilonGreedyAgent] = None,
                 agent_rewards: Optional[List[Callable[[Self], float]]] = []):
        """Create a MAP-Elites object.

        Args:
            lsystem (LSystem): The L-system used to expand strings.
            feasible_fitnesses (List[Fitness]): The list of fitnesses used.
            buffer (Buffer): The data buffer.
            behavior_descriptors (Tuple[BehaviorCharacterization, BehaviorCharacterization]): The X- and Y-axis behavior descriptors.
            n_bins (Tuple[int, int], optional): The number of X and Y bins. Defaults to `(8, 8)`.
            estimator (Optional[Union[GaussianEstimator, MLPEstimator, QuantileEstimator]], optional): The estimator used as fitness acquirement for the infeasible population. Defaults to `None`.
            emitter (Optional[Emitter], optional): The emitter. Defaults to `RandomEmitter()`.
            agent (Optional[EpsilonGreedyAgent], optional): The selection agent. Defaults to `None`.
            agent_rewards (Optional[List[Callable[[Self], float]]], optional): The rewards for the selection agent. Defaults to `[]`.

        Raises:
            AssertionError: Raised if an invalid configuration of properties is passed.
        """
        assert agent is not None or emitter is not None, 'MAP-Elites requires either an agent or an emitter!'
        if agent is not None and not agent_rewards:
            raise AssertionError(
                f'You selected an agent but no reward functions have been provided!')

        self.lsystem = lsystem
        self.feasible_fitnesses = feasible_fitnesses
        self.b_descs = behavior_descriptors
        self.emitter = emitter
        self.agent = agent
        self.agent_rewards = agent_rewards
        self.estimator = estimator
        self.buffer = buffer
        # number of total soft constraints
        self.nsc = [c for c in self.lsystem.all_hl_constraints if c.level == ConstraintLevel.SOFT_CONSTRAINT]
        self.nsc = [c for c in self.lsystem.all_ll_constraints if c.level == ConstraintLevel.SOFT_CONSTRAINT]
        self.nsc = len(self.nsc) * 0.5
        # behavior map properties
        self.limits = (self.b_descs[0].bounds[1] if self.b_descs[0].bounds is not None else 20,
                       self.b_descs[1].bounds[1] if self.b_descs[1].bounds is not None else 20)
        self._initial_n_bins = n_bins
        self.bin_qnt = n_bins
        self.bin_sizes = [[self.limits[0] / self.bin_qnt[0]] * n_bins[0], [self.limits[1] / self.bin_qnt[1]] * n_bins[1]]
        self.bins: npt.NDarray[MAPBin] = np.empty(shape=self.bin_qnt, dtype=MAPBin)
        for (i, j), _ in np.ndenumerate(self.bins):
            self.bins[i, j] = MAPBin(bin_idx=(i, j),
                                     bin_size=(self.bin_sizes[0][i], self.bin_sizes[1][j]))
        # enforce choosing only one bin at the time
        self.enforce_qnt = True
        # default hull builder
        self.hull_builder = HullBuilder(erosion_type='bin',
                                        apply_erosion=True,
                                        apply_smoothing=False)
        # fitness bounds
        self.max_f_fitness = sum([f.bounds[1] for f in self.feasible_fitnesses])
        self.max_i_fitness = len(self.lsystem.all_hl_constraints) if not self.estimator else 1
        self.infeas_fitness_idx = 1  # 0: min, 1: median, 2: max
        # MAP-Elites properties
        self.allow_res_increase = True
        self.allow_aging = True
        # tracking properties
        self.n_new_solutions = 0
        
        self.x_range, self.y_range, self.z_range = X_RANGE, Y_RANGE, Z_RANGE
        
    def show_metric(self,
                    metric: str,
                    show_mean: bool = True,
                    population: str = 'feasible',
                    save_as: Optional[str] = None) -> None:
        """Show the bin metric.

        Args:
            metric (str): The metric to display.
            show_mean (bool, optional): Whether to show average metric or elite's. Defaults to `True`.
            population (str, optional): Which population to show metric of. Defaults to `'feasible'`.
            save_as (str, optional): Where to save the metric plot. Defaults to `None`.
        """
        disp_map = np.zeros(shape=self.bins.shape)
        for (i, j), cbin in np.ndenumerate(self.bins):
            disp_map[i, j] = cbin.get_metric(metric=metric,
                                             use_mean=show_mean,
                                             population=population)
        vmaxs = {
            'fitness': {
                'feasible': self.max_f_fitness,
                'infeasible': self.max_i_fitness
            },
            'age': {
                'feasible': CS_MAX_AGE,
                'infeasible': CS_MAX_AGE
            },
            'size': {
                'feasible': BIN_POP_SIZE,
                'infeasible': BIN_POP_SIZE
            }
        }
        plt.imshow(disp_map,
                   origin='lower',
                   cmap='hot',
                   interpolation='nearest',
                   vmin=0,
                   vmax=vmaxs[metric][population])
        plt.xticks(np.arange(self.bin_qnt[0]), np.cumsum(self.bin_sizes[0]) + self.b_descs[0].bounds[0])
        plt.yticks(np.arange(self.bin_qnt[1]), np.cumsum(self.bin_sizes[1]) + self.b_descs[1].bounds[0])
        plt.xlabel(self.b_descs[0].name)
        plt.ylabel(self.b_descs[1].name)
        plt.title(f'CMAP-Elites {"Avg. " if show_mean else ""}{metric} ({population})')
        cbar = plt.colorbar()
        cbar.set_label(
            f'{"mean" if show_mean else "max"} {metric}', rotation=270)
        if save_as:
            title_part = f'{metric}{"-avg-" if show_mean else "-top-"}{population}'
            plt.savefig(f'results/{save_as}-{title_part}.png',
                        transparent=True, bbox_inches='tight')
        plt.show()

    def compute_fitness(self,
                        cs: CandidateSolution) -> float:
        """Compute the fitness of the solution. 

        Args:
            cs (CandidateSolution): The solution.

        Raises:
            NotImplementedError: Raised if the estimator is not recognized.

        Returns:
            float: The fitness value.
        """
        if cs.is_feasible:
            cs.fitness = [f(cs) for f in self.feasible_fitnesses]
            cs.representation = cs.fitness[:]
            x, y, z = cs.content._max_dims
            cs.representation.extend([x / MAX_X_SIZE, y / MAX_Y_SIZE, z / MAX_Z_SIZE])
            return sum([self.feasible_fitnesses[i].weight * cs.fitness[i] for i in range(len(cs.fitness))])
        else:
            cs.representation = [f(cs) for f in [Fitness(name='BoxFilling', f=box_filling_fitness, bounds=(0, 1)),
                                                 Fitness(name='FuncionalBlocks', f=func_blocks_fitness, bounds=(0, 1)),
                                                 Fitness(name='MajorMediumProportions', f=mame_fitness, bounds=(0, 1)),
                                                 Fitness(name='MajorMinimumProportions', f=mami_fitness, bounds=(0, 1))]]
            if self.estimator is not None:
                if isinstance(self.estimator, GaussianEstimator):
                    return self.estimator.predict(x=np.asarray(cs.representation)) if self.estimator.is_trained else EPSILON_F
                elif USE_TORCH and isinstance(self.estimator, MLPEstimator):
                    return self.estimator.predict(cs.representation) if self.estimator.is_trained else EPSILON_F
                elif USE_TORCH and isinstance(self.estimator, QuantileEstimator):
                    if self.estimator.is_trained:
                        # set fitness to (3,) array (min, median, max)
                        cs.fitness = self.estimator.predict(cs.representation)
                        # set c_fitness to median by default
                        return cs.fitness[self.infeas_fitness_idx]
                    else:
                        cs.fitness = [EPSILON_F, EPSILON_F, EPSILON_F]
                        return cs.fitness[self.infeas_fitness_idx]
                else:
                    raise NotImplementedError(f'Unrecognized estimator type: {type(self.estimator)}.')
            else:
                return cs.ncv

    def _assign_fitness(self,
                        cs: CandidateSolution) -> CandidateSolution:
        """Assign the fitness and BCs to a candidate solution.

        Args:
            cs (CandidateSolution): The candidate solution.

        Returns:
            CandidateSolution: The updated candidate solution.
        """
        # assign fitness
        cs.c_fitness = self.compute_fitness(cs=cs) + ((self.nsc - cs.ncv) if cs.is_feasible else 0)
        # assign behavior descriptors
        self._set_behavior_descriptors(cs=cs)
        # set age
        cs.age = CS_MAX_AGE
        return cs

    def _prepare_cs_content(self,
                            cs: CandidateSolution,
                            add_hull: bool = True) -> CandidateSolution:
        """Prepare a candidate solution for fitness computation.
        Add a hull if possible and set the blocks colors.

        Args:
            cs (CandidateSolution): The candidate solution.

        Returns:
            CandidateSolution: The updated candidate solution.
        """
        # add hull if possible
        if self.hull_builder and add_hull:
            threadsafe_hullbuilder = HullBuilder(erosion_type=self.hull_builder.erosion_type,
                                                 apply_erosion=self.hull_builder.apply_erosion,
                                                 apply_smoothing=self.hull_builder.apply_smoothing)
            threadsafe_hullbuilder.add_external_hull(structure=cs.content)
        # set the color
        cs.content.set_color(color=cs.base_color)
        # rotate according to tileset orientation
        # TODO: this is hardcoded, but should be read from a config file
        cs.content.rotate(along=1, k=3)
        cs.content_size = cs.content._max_dims
        logging.getLogger('mapelites').debug(f'[{__name__}._prepare_cs_content] Prepared {cs.string} ({add_hull=}).')
        return cs
    
    def _set_behavior_descriptors(self,
                                  cs: CandidateSolution) -> None:
        """Set the behavior descriptors of the solution.

        Args:
            cs (CandidateSolution): The candidate solution.
        """
        cs.b_descs = (self.b_descs[0](cs), self.b_descs[1](cs))

    def subdivide_range(self,
                        bin_idx: Tuple[int, int]) -> None:
        """Subdivide the chosen bin range.
        For each bin, 4 new bins are created and the solutions are redistributed amongst those.

        Args:
            bin_idx (Tuple[int, int]): The index of the bin.
        """
        i, j = bin_idx
        # get solutions
        all_cs = []
        for (_, _), cbin in np.ndenumerate(self.bins):
            all_cs.extend([*cbin._feasible, *cbin._infeasible])
        logging.getLogger('mapelites').debug(f'[{__name__}.subdivide_range] Starting subdivision at {bin_idx=}; {self.bins.shape=}.')
        # update bin sizes
        v_i, v_j = self.bin_sizes[0][i], self.bin_sizes[1][j]
        self.bin_sizes[0][i] = v_i / 2
        self.bin_sizes[1][j] = v_j / 2
        self.bin_sizes[0].insert(i + 1, v_i / 2)
        self.bin_sizes[1].insert(j + 1, v_j / 2)
        # update bin quantity
        self.bin_qnt = (self.bin_qnt[0] + 1, self.bin_qnt[1] + 1)
        # create new bin map
        new_bins = np.empty(shape=self.bin_qnt, dtype=MAPBin)        
        # populate newly created bins
        for (m, n), _ in np.ndenumerate(new_bins):
            new_bins[m, n] = MAPBin(bin_idx=(m, n),
                                    bin_size=(self.bin_sizes[0][m],
                                              self.bin_sizes[1][n]),
                                    bin_initial_size=(v_i, v_j))
            if m != i + 1 or n != j + 1:
                x = m if m <= i else m - 1
                y = n if n <= j else n - 1
            else:
                x = i
                y = j
            new_bins[m, n].new_elite = self.bins[x, y].new_elite.copy()
        # assign new bin map
        self.bins = new_bins
        logging.getLogger('mapelites').debug(f'[{__name__}.subdivide_range] Completed subdivision at {bin_idx=}; {self.bins.shape=}.')
        # assign solutions to bins
        self._update_bins(lcs=all_cs)
        if isinstance(self.emitter, HumanPrefMatrixEmitter):
            self.emitter._increase_preferences_res(idx=bin_idx)

    def _update_bins(self,
                     lcs: List[CandidateSolution]) -> None:
        """Update the bins by assigning new solutions.

        Args:
            lcs (List[CandidateSolution]): The list of new solutions.
        """
        bc0 = np.cumsum([0] + self.bin_sizes[0][:-1]) + self.b_descs[0].bounds[0]
        bc1 = np.cumsum([0] + self.bin_sizes[1][:-1]) + self.b_descs[1].bounds[0]
        logging.getLogger('mapelites').debug(f'[{__name__}._update_bins] Started updating bins...')
        for cs in lcs:
            b0, b1 = cs.b_descs
            i = np.digitize(x=[b0], bins=bc0, right=False)[0] - 1
            j = np.digitize(x=[b1], bins=bc1, right=False)[0] - 1
            self.bins[i, j].insert_cs(cs)
        if self.allow_aging:
            for (_, _), b in np.ndenumerate(self.bins):
                b.remove_old()

    def _age_bins(self,
                  diff: int = -1) -> None:
        """Age all bins.

        Args:
            diff (int, optional): The quantity to age for. Defaults to `-1`.
        """
        if self.allow_aging:
            for (_, _), cbin in np.ndenumerate(self.bins):
                cbin.age(diff=diff)

    def _valid_bins(self) -> List[MAPBin]:
        """Get all the valid bins. A valid bin is a bin with at least 2 Feasible solution and 2 Infeasible solution.

        Returns:
            List[MAPBin]: The list of valid bins.
        """
        return [cbin for (_, _), cbin in np.ndenumerate(self.bins) if len(cbin._feasible) > 0]

    def _check_res_trigger(self) -> List[Tuple[int, int]]:
        """Trigger a resolution increase if at least 1 bin has reached full population capacity for both Feasible and Infeasible populations.

        Returns:
            List[Tuple[int, int]]: The indices of bins that were subdivided.
        """
        if self.allow_res_increase:
            to_increase_res = [cbin.bin_idx for (_, _), cbin in np.ndenumerate(self.bins) if len(cbin._feasible) >= BIN_POP_SIZE and len(cbin._infeasible) >= BIN_POP_SIZE and cbin.subdividable]
            for bin_idx in reversed(to_increase_res):
                self.subdivide_range(bin_idx=bin_idx)
            return to_increase_res
        return []

    def _process_expanded_idxs(self,
                               expanded_idxs: List[Tuple[int, int]],
                               selected_idxs: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """Process the indices of expanded bins and filter out expanded bins that were not selected.

        Args:
            expanded_idxs (List[Tuple[int, int]]): The indices of expanded bins.
            selected_idxs (List[Tuple[int, int]]): The indices of selected bins.

        Returns:
            List[Tuple[int, int]]: The processed indices of expanded bins.
        """
        logging.getLogger('mapelites').debug(msg=f'[{__name__}._process_expanded_idxs] pre {selected_idxs=}; {expanded_idxs=}')
        processed_idxs = [idx for idx in selected_idxs]
        for (m, n) in selected_idxs:
            for (i, j) in expanded_idxs:
                if m == i:
                    processed_idxs.append((m + 1, n))
                if n == j:
                    processed_idxs.append((m, n + 1))
                if m == i and n == j:
                    processed_idxs.append((m + 1, n + 1))
        for i, (m, n) in enumerate(processed_idxs):
            lm = len([x[0] for x in expanded_idxs if x[0] < m])
            ln = len([x[1] for x in expanded_idxs if x[1] < n])
            processed_idxs[i] = (m + lm, n + ln)
        logging.getLogger('mapelites').debug(msg=f'[{__name__}._process_expanded_idxs] post {processed_idxs=}')
        return processed_idxs

    def update_behavior_descriptors(self,
                                    bs: Tuple[BehaviorCharacterization]) -> None:
        """Update the behavior descriptors used in the MAP-Elites.

        Args:
            bs (Tuple[BehaviorCharacterization]): The 2 new behavior descriptors.
        """
        self.b_descs = bs
        self.limits = (self.b_descs[0].bounds[1] if self.b_descs[0].bounds is not None else 20,
                       self.b_descs[1].bounds[1] if self.b_descs[1].bounds is not None else 20)
        lcs = []
        for (_, _), cbin in np.ndenumerate(self.bins):
            lcs.extend([*cbin._feasible, *cbin._infeasible])
            Parallel(n_jobs=-1, prefer="threads")(delayed(self._set_behavior_descriptors)(cs) for cs in lcs)
        self.reset(lcs=lcs)

    def toggle_module_mutability(self,
                                 module: str) -> None:
        """Toggle the mutability of the module.

        Args:
            module (str): The module's name.
        """
        # toggle module's mutability in the solution
        for (_, _), cbin in np.ndenumerate(self.bins):
            cbin.toggle_module_mutability(module=module)
        # toggle module's mutability within the L-system
        ms = [x.name for x in self.lsystem.modules]
        self.lsystem.modules[ms.index(module)].active = not self.lsystem.modules[ms.index(module)].active

    def update_fitness_weights(self,
                               weights: List[float]) -> None:
        """Update the weights of the Feasible fitnesses.

        Args:
            weights (List[float]): The new list of weights.
        """
        assert len(weights) == len(self.feasible_fitnesses), f'Wrong number of weights ({len(weights)}) for fitnesses ({len(self.feasible_fitnesses)}) passed.'
        # update weights
        for w, f in zip(weights, self.feasible_fitnesses):
            f.weight = w
        # update solutions fitnesses
        for (_, _), cbin in np.ndenumerate(self.bins):
            for cs in cbin._feasible:
                cs.c_fitness = sum([self.feasible_fitnesses[i].weight * cs.fitness[i] for i in range(len(cs.fitness))]) + (self.nsc - cs.ncv)

    def _within_range(self,
                      cs: CandidateSolution) -> bool:
        """Check if the solution dimensions are within the allowed range.

        Args:
            cs (CandidateSolution): The candidate solution.

        Returns:
            bool: Whether the solution dimensions are within the allowed range.
        """
        cs_dims = cs.content._max_dims if cs._content else cs.content_size
        return self.x_range[0] <= cs_dims[0] <= self.x_range[1] and self.y_range[0] <= cs_dims[1] <= self.y_range[1] and self.z_range[0] <= cs_dims[2] <= self.z_range[1]
    
    def generate_initial_populations(self,
                                     pop_size: int = POP_SIZE,
                                     n_retries: int = N_RETRIES) -> None:
        """Generate the initial populations.

        Args:
            pop_size (int, optional): The size of the populations. Defaults to `POP_SIZE`.
            n_retries (int, optional): The number of initialization retries. Defaults to `N_RETRIES`.
        """
        # create populations
        feasible_pop, infeasible_pop = [], []
        self.lsystem.disable_sat_check()
        with trange(n_retries, desc='Initialization ') as iterations:
            for i in iterations:
                solutions = self.lsystem.apply_rules(starting_strings=['head', 'body', 'tail'],
                                                     iterations=[1, N_ITERATIONS, 1],
                                                     create_structures=True,
                                                     make_graph=False)
                subdivide_solutions(lcs=solutions,
                                    lsystem=self.lsystem)
                for cs in solutions:
                    self._prepare_cs_content(cs=cs,
                                             add_hull=False)
                    if self._within_range(cs=cs):
                        if cs.is_feasible and len(feasible_pop) < pop_size and cs not in feasible_pop:
                            if self.hull_builder is not None:
                                self.hull_builder.add_external_hull(structure=cs._content)
                            feasible_pop.append(self._assign_fitness(cs=cs))
                        elif not cs.is_feasible and len(infeasible_pop) < pop_size and cs not in feasible_pop:
                            infeasible_pop.append(self._assign_fitness(cs=cs))
                iterations.set_postfix(ordered_dict={
                    'fpop-size': f'{len(feasible_pop)}/{pop_size}',
                    'ipop-size': f'{len(infeasible_pop)}/{pop_size}'
                },
                                       refresh=True)
                if i == n_retries or (len(feasible_pop) == pop_size and len(infeasible_pop) == pop_size):
                    break
        # assign solutions to respective bins
        self._update_bins(lcs=[*feasible_pop, *infeasible_pop])
        # update bins for elites
        self.update_elites()
        # if required, initialize the emitter
        if self.emitter is not None and self.emitter.requires_init:
            self.emitter.init_emitter(bins=self.bins)

    def _step(self,
              populations: List[List[CandidateSolution]],
              gen: int) -> List[CandidateSolution]:
        """Apply a single step of modified FI2Pop.

        Args:
            populations (List[List[CandidateSolution]]): The Feasible and Infeasible populations.
            gen (int): The current generation number.

        Raises:
            NotImplementedError: Raised if the estimator is unrecognized.

        Returns:
            List[CandidateSolution]: The new solutions.
        """
        # generate solutions from both populations
        generated: List[CandidateSolution] = []
        for pop in populations:
            logging.getLogger('mapelites').debug(msg=f'[{__name__}._step] {len(pop)=}')
            if len(pop) > 0:
                try:
                    minimize = False if pop[0].is_feasible else False if self.estimator is not None else True
                    new_pool = create_new_pool(population=pop,
                                               generation=gen,
                                               n_individuals=BIN_POP_SIZE,
                                               minimize=minimize)
                    # set low-level strings and structures
                    new_pool = list(map(lambda cs: self.lsystem._add_ll_strings(cs=cs), new_pool))
                    new_pool = list(map(lambda cs: self.lsystem._set_structure(cs=cs,
                                                                               make_graph=False), new_pool))
                    logging.getLogger('mapelites').debug(msg=f'[{__name__}._step] {len(new_pool)=}')
                    subdivide_solutions(lcs=new_pool,
                                        lsystem=self.lsystem)
                    logging.getLogger('mapelites').debug(msg=f'[{__name__}._step] Started preparing solutions')
                    new_pool = Parallel(n_jobs=-1, prefer="threads")(delayed(self._prepare_cs_content)(cs) for cs in new_pool)
                    logging.getLogger('mapelites').debug(msg=f'[{__name__}._step] Started assigning fitnesses')
                    generated.extend(Parallel(n_jobs=-1, prefer="threads")(delayed(self._assign_fitness)(cs) for cs in new_pool))
                # evoexceptions are ignored, though it is possible to get stuck here
                except EvoException as e:
                    logging.getLogger('mapelites').error(msg=f'[{__name__}._step] {e}')
                    pass
        
        generated = list(filter(self._within_range, generated))
        
        # if possible, train the estimator for fitness acquirement
        if self.estimator is not None:
            # Prepare dataset for estimator
            xs, ys = prepare_dataset(f_pop=[x for x in generated if x.is_feasible])
            for x, y in zip(xs, ys):
                self.buffer.insert(x=x,
                                   y=y / self.max_f_fitness)
            # check if we can train the estimator
            try:
                xs, ys = self.buffer.get()
                if isinstance(self.estimator, GaussianEstimator):
                    self.estimator.fit(xs=xs, ys=ys)
                elif USE_TORCH and isinstance(self.estimator, MLPEstimator) or isinstance(self.estimator, QuantileEstimator):
                    train_estimator(self.estimator, xs=xs, ys=ys)
                else:
                    raise NotImplementedError(f'Unrecognized estimator type {type(self.estimator)}.')
            # we skip training altogether if we don't have datapoints
            except EmptyBufferException:
                logging.getLogger('mapelites').debug(f'[{__name__}._step] Skipped training; no datapoints in buffer.')
                pass
            # realignment check
            logging.getLogger('mapelites').debug(f'[{__name__}._step] Started realignment...')
            if self.estimator.is_trained and gen % ALIGNMENT_INTERVAL == 0:
                # Reassign previous infeasible fitnesses
                for (_, _), cbin in np.ndenumerate(self.bins):
                    for cs in cbin._infeasible:
                        if cs.age > ALIGNMENT_INTERVAL:
                            if cs._content is None:
                                self.lsystem._set_structure(cs=cs,
                                                            make_graph=False)
                            self._prepare_cs_content(cs)
                            cs.c_fitness = self.compute_fitness(cs=cs)
        # metrics tracking
        self.n_new_solutions += len(generated)
        return generated

    def rand_step(self,
                  gen: int = 0) -> None:
        """Apply a random step.

        Args:
            gen (int, optional): The current number of generations. Defaults to 0.
        """
        # trigger aging of solution
        self._age_bins()
        # pick random bin
        rnd_bin = random.choice(self._valid_bins())
        generated = self._step(populations=[rnd_bin._feasible, rnd_bin._infeasible],
                               gen=gen)
        if generated:
            self._update_bins(lcs=generated)
            self._check_res_trigger()
        else:
            self._age_bins(diff=1)

    def seek_nearest_valid(self,
                           bin_idxs: List[List[int]],
                           pop: str = 'infeasible') -> List[CandidateSolution]:
        """Collect CandidateSolutions from neighbouring indexes in a pseudo-nearestneighbour style.

        Args:
            bin_idxs (List[List[int]]): The list of bin indices.
            pop (str, optional): The population to collect solutions for. Defaults to 'infeasible'.

        Returns:
            List[CandidateSolution]: The list of neighbouring solutions, if it exists.
        """
        new_pop = []
        inspecting = [idx for idx in bin_idxs]
        offsets = [(-1, -1), (-1, 0), (-1,1), (0, -1), (0, 1), (1, -1),  (1, 0), (1, 1)]
        np.random.shuffle(offsets)
        logging.getLogger('mapelites').debug(msg=f'[{__name__}.seek_nearest_valid] {pop=}; {inspecting=}')
        while new_pop == []:
            to_inspect = []
            past_idxs = []
            for idx in inspecting:
                for offset in offsets:
                    new_idx = (idx[0] + offset[0], idx[1] + offset[1])
                    if 0 <= new_idx[0] < self.bins.shape[0] and 0 <= new_idx[1] < self.bins.shape[1]:
                        pool = self.bins[new_idx]._feasible if pop == 'feasible' else self.bins[new_idx]._infeasible
                        if pool:
                            new_pop.extend(pool) 
                            break
                        else:
                            if idx not in past_idxs:
                                to_inspect.append(new_idx)
            if to_inspect == []:
                break
            inspecting = to_inspect
        logging.getLogger('mapelites').debug(msg=f'[{__name__}.seek_nearest_valid] {new_pop=}')
        return new_pop
    
    def interactive_step(self,
                         bin_idxs: List[List[int]],
                         gen: int = 0) -> float:
        """Applies an interactive step.

        Args:
            bin_idxs (List[Tuple[int, int]]): The indexes of the bins selected.
            gen (int, optional): The current number of generations. Defaults to 0.

        Returns:
            float: Time elapsed by the emitter (if it exists) in the `pre_step`.
        """
        self._age_bins()
        bin_idxs = [tuple(b) for b in bin_idxs]
        logging.getLogger('mapelites').debug(f'[{__name__}.interactive_step] Picked {bin_idxs=}.')
        chosen_bins = [self.bins[bin_idx] for bin_idx in bin_idxs]
        f_pop, i_pop = [], []
        for chosen_bin in chosen_bins:
            if self.enforce_qnt:
                assert chosen_bin in self._valid_bins(), f'Bin at {chosen_bin.bin_idx} is not a valid bin.'
            f_pop.extend(chosen_bin._feasible)
            i_pop.extend(chosen_bin._infeasible)
        if i_pop == []:
            i_pop = self.seek_nearest_valid(bin_idxs=bin_idxs,
                                            pop='infeasible')
        generated = self._step(populations=[f_pop, i_pop],
                               gen=gen)
        if generated:
            self._update_bins(lcs=generated)
            expanded_idxs = self._check_res_trigger()
            # keep track of expanded indexes only if they have also been selected
            expanded_idxs = self._process_expanded_idxs(expanded_idxs=expanded_idxs,
                                                        selected_idxs=bin_idxs)
        else:
            self._age_bins(diff=1)
            expanded_idxs = []
        s = time.perf_counter()
        if self.emitter is not None and self.emitter.requires_pre:
            logging.getLogger('mapelites').debug(f'[{__name__}.interactive_step] Started {self.emitter.name} pre step.')
            self.emitter.pre_step(bins=self.bins,
                                  selected_idxs=bin_idxs,
                                  expanded_idxs=expanded_idxs,
                                  bounds=[b.bounds for b in self.b_descs])
        return time.perf_counter() - s
        
    def emitter_step(self,
                     gen: int = 0) -> None:
        """Apply a step according to the emitter.

        Args:
            gen (int, optional): The current generation number. Defaults to `0`.

        Returns:
            float: Time elapsed by the emitter (if it exists) for sampling a bin and running `post_step`.
        
        Raises:
            AssertionError: Raised if there is no MultiArmed Bandit Agent or Emitter set in MAP-Elites.
            NotImplementedError: Raised if the merge method specified in the bandit action is unrecognized.
            NotImplementedError: Raised if the emitter output is not in the expected data format.
        """
        emitter_time = 0
        assert self.agent or self.emitter, 'MAP-Elites requires either a fixed emitter or a MultiArmed Bandit Agent, but neither were provided.'
        if self.agent is not None:
            # get bandit
            bandit = self.agent.choose_bandit()
            emitter_str, method_str = bandit.action.split(';')
            # set emitter
            self.emitter = get_emitter_by_str(emitter=emitter_str)
            # set merge method
            if method_str == 'max':
                self.infeas_fitness_idx = 0
            elif method_str == 'median':
                self.infeas_fitness_idx = 1
            elif method_str == 'min':
                self.infeas_fitness_idx = 2
            else:
                raise NotImplementedError(f'Unrecognized merge method from bandit action: {method_str}')
            # update existing solution's fitness
            for (_, _), cbin in np.ndenumerate(self.bins):
                for cs in cbin._infeasible:
                    cs.c_fitness = cs.fitness[self.infeas_fitness_idx]
        s = time.perf_counter()
        selected_bins = self.emitter.pick_bin(bins=self.bins)
        emitter_time += time.perf_counter() - s
        logging.getLogger('mapelites').debug(msg=f'[{__name__}.emitter_step] {selected_bins=}')
        if selected_bins:
            fpop, ipop = [], []
            # TODO: this could be handled better
            if isinstance(selected_bins[0], MAPBin):
                for selected_bin in selected_bins:
                    fpop.extend(selected_bin._feasible)
                    ipop.extend(selected_bin._infeasible)
            elif isinstance(selected_bins[0], list):
                for selected_bin in selected_bins[0]:
                    fpop.extend(selected_bin._feasible)
                for selected_bin in selected_bins[1]:
                    ipop.extend(selected_bin._infeasible)
            else:
                raise NotImplementedError(f'Unrecognized emitter output: {selected_bins}.')
            logging.getLogger('mapelites').debug(msg=f'[{__name__}.emitter_step] {len(fpop)=}; {len(ipop)=}')
            if ipop == []:
                if isinstance(selected_bins[0], MAPBin):
                    ipop = self.seek_nearest_valid(bin_idxs=[b.bin_idx for b in selected_bins],
                                                    pop='infeasible')
                elif isinstance(selected_bins[0], list):
                    ipop = self.seek_nearest_valid(bin_idxs=[b.bin_idx for b in selected_bins[1]],
                                                    pop='infeasible')
            generated = self._step(populations=[fpop, ipop],
                                   gen=gen)
            if generated:
                self._update_bins(lcs=generated)
                self._check_res_trigger()
            s = time.perf_counter()
            if self.emitter is not None and self.emitter.requires_post:
                self.emitter.post_step(bins=self.bins)
            emitter_time += time.perf_counter() - s
            if self.agent is not None:
                self.agent.reward_bandit(bandit=bandit,
                                         reward=sum([f(self) for f in self.agent_rewards]))
        return emitter_time

    def update_valid_ranges(self,
                            x_range: Tuple[int, int],
                            y_range: Tuple[int, int],
                            z_range: Tuple[int, int]) -> None:
        """Update the valid ranges of the spaceships' dimensions, and update the population.

        Args:
            x_range (Tuple[int, int]): The new range for the X axis.
            y_range (Tuple[int, int]): The new range for the Y axis.
            z_range (Tuple[int, int]): The new range for the Z axis.
        """
        self.x_range, self.y_range, self.z_range = x_range, y_range, z_range
        for (_, _), b in np.ndenumerate(self.bins):
            b._feasible = list(filter(self._within_range, b._feasible))
            b._infeasible = list(filter(self._within_range, b._infeasible))
    
    
    def reset(self,
              lcs: Optional[List[CandidateSolution]] = None) -> None:
        """Reset the current MAP-Elites.

        Args:
            lcs (Optional[List[CandidateSolution]], optional): If provided, the solutions are assigned to the new MAP-Elites. Defaults to `None`.
        """
        # reset MAP-Elites properties
        self.bin_qnt = self._initial_n_bins
        self.bin_sizes = [[self.limits[0] / self.bin_qnt[0]] * self._initial_n_bins[0],
                          [self.limits[1] / self.bin_qnt[1]] * self._initial_n_bins[1]]
        self.bins = np.empty(shape=self.bin_qnt, dtype=MAPBin)
        for (i, j), _ in np.ndenumerate(self.bins):
            self.bins[i, j] = MAPBin(bin_idx=(i, j),
                                     bin_size=(self.bin_sizes[0][i], self.bin_sizes[1][j]))
        if self.estimator is not None:
            if isinstance(self.estimator, GaussianEstimator):
                self.estimator = GaussianEstimator(bound=self.estimator.bound,
                                                   kernel=self.estimator.kernel,
                                                   max_f=self.estimator.max_f,
                                                   min_f=self.estimator.min_f,
                                                   alpha=self.estimator.alpha,
                                                   normalize_y=self.estimator.normalize_y)
            if USE_TORCH and isinstance(self.estimator, MLPEstimator):
                self.estimator = MLPEstimator(xshape=self.estimator.xshape,
                                              yshape=self.estimator.yshape)
            elif USE_TORCH and isinstance(self.estimator, QuantileEstimator):
                self.estimator = QuantileEstimator(xshape=self.estimator.xshape,
                                                   yshape=self.estimator.yshape)
        self.buffer.clear()
        if self.emitter is not None:
            self.emitter.reset()
        # assign solutions if provided
        if lcs is not None:
            self._update_bins(lcs=lcs)
            self._check_res_trigger()
            if self.emitter is not None and self.emitter.requires_init:
                self.emitter.init_emitter(bins=self.bins)
        else:
            self.generate_initial_populations()

    def save_population(self,
                        filename: str = './population.pop') -> None:
        all_cs = []
        for (_, _), b in np.ndenumerate(self.bins):
            for cs in [*b._feasible, *b._infeasible]:
                all_cs.append(cs.to_json())
        with open(filename, 'w') as f:
            f.write(json_dumps(all_cs))
       
    def load_population(self,
                        filename: str = './population.pop') -> None:
        all_cs = []
        with open(filename, 'r') as f:
            all_cs = [CandidateSolution.from_json(x) for x in json_loads(f.read())]
        # add to population
        self._update_bins(lcs=all_cs)
        # update bins for elites
        self.update_elites()
        for (_, _), b in np.ndenumerate(self.bins):
            for pop in ['feasible', 'infeasible']:
                if b.non_empty(pop=pop):
                    e = b.get_elite(population=pop)
                    if e._content is None:
                        self.lsystem._set_structure(cs=e, make_graph=False)
                        self._prepare_cs_content(cs=e)
        # initialise emitter if needed
        if self.emitter is not None and self.emitter.requires_init:
                self.emitter.init_emitter(bins=self.bins)
    
    def update_elites(self,
                      reset: bool = False):
        """Update the elite tracking for the MAPElites bins.

        Args:
            reset (bool, optional): Whether to reset the tracking. Defaults to False.
        """
        for (_, _), b in np.ndenumerate(self.bins):
            for pop in ['feasible', 'infeasible']:
                if reset:
                    b.new_elite[pop] = False
                else:
                    b.check_new_elite(pop=pop)
                # logging.getLogger('mapelites').debug(f'[{__name__}.update_elites] {reset=}; {b.bin_idx} -> {b.new_elite[pop]}')
            
    def population_complexity(self,
                              pop: str = 'feasible') -> float:
        """Compute the average complexity of the selected population.
        The complexity in this case is defined as the number of blocks.

        Returns:
            float: The population complexity.
        """
        all_lenghts = []
        for (_, _), b in np.ndenumerate(self.bins):
            for cs in b._feasible if pop == 'feasible' else b._infeasible:
                all_lenghts.append(cs.n_blocks)
        return np.average(all_lenghts)
    
    
    def total_solutions(self,
                        pop: str = 'feasible') -> int:
        """Compute the total number of solutions currently in the archive for the selected population.

        Args:
            pop (str, optional): The population type. Defaults to 'feasible'.

        Returns:
            int: The total number of solutions in the archive for the selected population.
        """
        return np.sum([b.get_metric(metric='size', population=pop) for (_, _), b in np.ndenumerate(self.bins)])
    
    
    def to_json(self) -> Dict[str, Any]:
        return {
            'lsystem': self.lsystem.to_json(),
            'feasible_fitnesses': [f.to_json() for f in self.feasible_fitnesses],
            'b_descs': [bd.to_json() for bd in list(self.b_descs)],
            'emitter': self.emitter.to_json() if self.emitter else None,
            'agent': self.agent.to_json() if self.agent else None,
            'agent_rewards': [ar.__name__ for ar in self.agent_rewards],
            'initial_n_bins': list(self._initial_n_bins),
            'bin_qnt': list(self.bin_qnt),
            'bins': [b.to_json() for b in self.bins.flatten().tolist()],
            'enforce_qnt': self.enforce_qnt,
            'estimator_type': self.estimator.__class__.__name__ if self.estimator else None,
            'estimator': self.estimator.to_json() if self.estimator else None,
            'buffer': self.buffer.to_json(),
            'allow_res_increase': self.allow_res_increase,
            'allow_aging': self.allow_aging,
        }

    @staticmethod
    def from_json(my_args: Dict[str, Any]) -> 'MAPElites':
        me = MAPElites(lsystem=LSystem.from_json(my_args['lsystem']),
                       feasible_fitnesses=[Fitness.from_json(
                           f) for f in my_args['feasible_fitnesses']],
                       buffer=Buffer.from_json(my_args['buffer']),
                       behavior_descriptors=tuple(
                           [BehaviorCharacterization.from_json(bc) for bc in my_args['b_descs']]),
                       n_bins=tuple(my_args['bin_qnt']),
                       estimator=None,
                       emitter=RandomEmitter(),
                       agent=None,
                       agent_rewards=None)
        me._initial_n_bins = my_args['initial_n_bins']
        me.enforce_qnt = my_args['enforce_qnt']
        me.allow_res_increase = my_args['allow_res_increase']
        me.allow_aging = my_args['allow_aging']
        if my_args['emitter']:
            me.emitter = emitters[my_args['emitter']
                                  ['name']].from_json(my_args['emitter'])
        if my_args['estimator_type']:
            estimators = {
                'GaussianEstimator': GaussianEstimator,
                'MLPEstimator': MLPEstimator,
                'QuantileEstimator': QuantileEstimator
            }
            me.estimator = estimators[my_args['estimator_type']].from_json(my_args['estimator'])
        if my_args['agent']:
            me.agent = EpsilonGreedyAgent.from_json(my_args['agent'])
            me.agent_rewards = [agent_rewards[ar]
                                for ar in my_args['agent_rewards']]
        me.bins = np.asarray([MAPBin.from_json(mb)
                             for mb in my_args['bins']]).reshape(me.bin_qnt)
        return me

    # This method is deprecated and was used during initial debugging. Source is kept jsut in case.
    # def interactive_mode(self,
    #                      n_steps: int = 10) -> None:
    #     """Start an interactive evolution session. Bins choice is done via `input`.

    #     Args:
    #         n_steps (int, optional): The number of steps to evolve for. Defaults to 10.
    #     """
    #     for n in range(n_steps):
    #         print(f'### STEP {n+1}/{n_steps} ###')
    #         valid_bins = self._valid_bins()
    #         list_valid_bins = '\n-' + '\n-'.join([str(x.bin_idx) for x in valid_bins])
    #         print(f'Valid bins are: {list_valid_bins}')
    #         chosen_bin = None
    #         while chosen_bin is None:
    #             choice = input('Enter valid bin to evolve: ')
    #             choice = choice.replace(' ', '').split(',')
    #             selected = self.bins[int(choice[0]), int(choice[1])]
    #             if selected in valid_bins:
    #                 chosen_bin = selected
    #             else:
    #                 print('Chosen bin is not amongst valid bins.')
    #         self._interactive_step(bin_idxs=[chosen_bin.bin_idx],
    #                                gen=n)


def get_elite(mapelites: MAPElites,
              bin_idx: Tuple[int, int],
              pop: str) -> CandidateSolution:
    """Get the elite solution at the selected bin.

    Args:
        mapelites (MAPElites): The MAP-Elites object.
        bin_idx (Tuple[int, int]): The index of the bin.
        pop (str): The population.

    Returns:
        CandidateSolution: The elite solution.
    """
    return mapelites.bins[bin_idx].get_elite(population=pop)


def get_coverage(mapelites: MAPElites,
                 pop: str) -> Tuple[int, int]:
    """Get the grid coverage.

    Args:
        mapelites (MAPElites): The MAP-Elites object.
        pop (str): The population.

    Returns:
        Tuple[int, int]: The number of non-empty bins and the total number of bins.
    """
    t = mapelites.bins.shape[0] * mapelites.bins.shape[1]
    c = sum([1 if cbin.non_empty(pop=pop) else 0 for (_, _), cbin in np.ndenumerate(mapelites.bins)])
    return c, t


def get_fitness_metrics(mapelites: MAPElites,
                        pop: str) -> Tuple[int, int]:
    """Get the fitness metrics of a population.

    Args:
        mapelites (MAPElites): The MAP-Elites object.
        pop (str): The population.

    Returns:
        Tuple[int, int]: The top and mean fitness.
    """
    fs = []
    for (_, _), cbin in np.ndenumerate(mapelites.bins):
        for cs in cbin._feasible if pop == 'feasible' else cbin._infeasible:
            fs.append(cs.c_fitness)
    top = min(fs) if pop == 'infeasible' and mapelites.estimator is None else max(fs)
    return top, np.average(fs)


def get_qdscore(mapelites: MAPElites,
                pop: str) -> float:
    """Get the Quality-Diversity-Score for the selected population. The QD-Score is computed as the sum of the fitness of the elite solution in each bin.

    Args:
        mapelites (MAPElites): The MAP-Elites object.
        pop (str): The population.

    Returns:
        float: The QD-Score.
    """
    return sum([cbin.get_elite(population=pop).c_fitness if cbin.non_empty(pop=pop) else 0 for (_, _), cbin in np.ndenumerate(mapelites.bins)])


def get_new_feas_with_unfeas_parents(mapelites: MAPElites) -> Tuple[int, int]:
    """Get the number of new feasible solutions with infeasible parents and the total number of new solutions.

    Args:
        mapelites (MAPElites): The MAP-Elites object.

    Returns:
        Tuple[int, int]: The number of new solutions with infeasible parents and the total number of new solutions
    """
    n_new = 0
    total = 0
    for (_, _), cbin in np.ndenumerate(mapelites.bins):
        for cs in cbin._feasible:
            if cs.age == CS_MAX_AGE and cs.parents and not cs.parents[0].is_feasible:
                n_new += 1
                total += 1
                break
        for cs in cbin._infeasible:
            if cs.age == CS_MAX_AGE:
                total += 1
    return n_new, total


def get_random_elite(mapelites: MAPElites,
                     pop: str) -> CandidateSolution:
    """Get a random elite for the selected population.

    Args:
        mapelites (MAPElites): The MAP-Elites object.
        pop (str): The population.

    Returns:
        CandidateSolution: The random elite.
    """
    return np.random.choice([cbin for (_, _), cbin in np.ndenumerate(mapelites.bins) if cbin.non_empty(pop=pop)]).get_elite(population=pop)
