from functools import cached_property
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from pcgsepy.common.vecs import Vec

from ..structure import Structure


class CandidateSolution:
    __slots__ = ['string', '_content', 'age', 'b_descs', 'c_fitness', 'fitness', 'hls_mod',
                 'is_feasible', 'll_string', 'n_feas_offspring', 'n_offspring', 'ncv',
                 'parents', 'representation', 'base_color', 'n_blocks', 'content_size']
    
    def __init__(self,
                 string: str,
                 content: Optional[Structure] = None):
        self.string: str = string
        self._content: Structure = content
        
        self.age: int = 0
        self.b_descs: Tuple[float, float] = (0., 0.)
        self.c_fitness: float = 0.
        self.fitness: List[float] = []
        self.hls_mod: Dict[str, Any] = {}  # keys: 'string', 'mutable'
        self.is_feasible: bool = True
        self.ll_string: str = ''
        self.n_feas_offspring: int = 0
        self.n_offspring: int = 0
        self.ncv: int = 0  # number of constraints violated
        self.parents: List[CandidateSolution] = []
        self.representation: List[float] = []
        self.base_color = Vec.v3f(x=0.45, y=0.45, z=0.45)  # default block color is #737373
        self.n_blocks = 0
        self.content_size = (0, 0, 0)

    def __str__(self) -> str:
        return f'{self.string}; fitness: {self.c_fitness}; is_feasible: {self.is_feasible}'

    def __repr__(self) -> str:
        return str(self)

    def __eq__(self,
               other: 'CandidateSolution') -> bool:
        if isinstance(other, CandidateSolution):
            return self.string == other.string
        return False

    def __hash__(self):
        return hash(self.string)

    def set_content(self,
                    content: Structure):
        """Set the content of the solution.

        Args:
            content (Structure): The content.

        Raises:
            Exception: Raised if the solution already has a content set.
        """
        if self._content:
            raise Exception('Structure already exists for this CandidateSolution.')
        else:
            self._content = content
            self.n_blocks = len(content._blocks)

    @property
    def content(self) -> Structure:
        if self._content:
            return self._content
        else:
            raise NotImplementedError('Structure has not been set yet.')
        
    @property
    def size(self) -> Tuple[int, int, int]:
        return self._content._max_dims
    
    @property
    def unique_blocks(self) -> Dict[str, int]:
        unique_blocks_dict = {
            'Gyroscopes': ['MyObjectBuilder_Gyro_LargeBlockGyro'],
            'Reactors': ['MyObjectBuilder_Reactor_LargeBlockSmallGenerator'],
            'Containers': ['MyObjectBuilder_CargoContainer_LargeBlockSmallContainer'],
            'Cockpits': ['MyObjectBuilder_Cockpit_OpenCockpitLarge'],
            'Thrusters': ['MyObjectBuilder_Thrust_LargeBlockSmallThrust'],
            'Lights': ['MyObjectBuilder_InteriorLight_SmallLight', 'MyObjectBuilder_InteriorLight_LargeBlockLight_1corner']
        }
        counts = {}
        for k, vs in unique_blocks_dict.items():
            counts[k] = 0
            for v in vs:
                counts[k] = counts[k] + self._content.unique_blocks_count(block_type=v)
        return counts
    
    def to_json(self) -> Dict[str, Any]:
        return {
            'string': self.string,
            'age': self.age,
            'b_descs': self.b_descs,
            'c_fitness': self.c_fitness,
            'fitness': self.fitness,
            'hls_mod': self.hls_mod,
            'is_feasible': self.is_feasible,
            'll_string': self.ll_string,
            'n_feas_offspring': self.n_feas_offspring,
            'n_offspring': self.n_offspring,
            'ncv': self.ncv,
            'parents': [p.to_json() for p in self.parents],
            'representation': self.representation
        }
    
    @staticmethod
    def from_json(my_args: Dict[str, Any]) -> 'CandidateSolution':
        cs = CandidateSolution(string=my_args['string'],
                               content=None)
        cs.age = my_args['age']
        cs.b_descs = my_args['b_descs']
        cs.c_fitness = my_args['c_fitness']
        cs.fitness = my_args['fitness']
        cs.hls_mod = my_args['hls_mod']
        cs.is_feasible = my_args['is_feasible']
        cs.ll_string = my_args['ll_string']
        cs.n_feas_offspring = my_args['n_feas_offspring']
        cs.n_offspring = my_args['n_offspring']
        cs.ncv = my_args['ncv']
        cs.parents = [CandidateSolution.from_json(args=p) for p in my_args['parents']]
        cs.representation = my_args['representation']
        return cs


def string_merging(ls: List[str]) -> str:
    """Merge a list of strings.

    Args:
        ls (List[str]): The list of strings.

    Returns:
        str: The merged string.
    """
    # any additional control on alignment etc. should be done here.
    return ''.join(s for s in ls)


def merge_solutions(lcs: List[CandidateSolution],
                    modules_names: List[str],
                    modules_active: List[bool]) -> CandidateSolution:
        """
        Merge solutions in a single solution, keeping track of modules' solutions.

        Args:
            lcs (List[CandidateSolution]): The list of solutions to merge, ordered.
            modules_names (List[str]): The name of the L-system modules.
            modules_active: (List[bool]): The default mutability of the L-system modules.

        Returns:
            CandidateSolution: The merged solution
        """
        assert len(lcs) == len(modules_names), f'Each solution should be produced by a module! Passed {len(lcs)} solutions and {len(modules_names)} modules.'
        m_cs = CandidateSolution(string=string_merging(ls=[cs.string for cs in lcs]))
        for i, (cs, default_m) in enumerate(zip(lcs, modules_active)):
            m_cs.hls_mod[modules_names[i]] = {'string': cs.string,
                                              'mutable': default_m}
        return m_cs
