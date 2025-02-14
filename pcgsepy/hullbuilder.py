import logging
from scipy.spatial import ConvexHull, Delaunay
from scipy.ndimage import grey_erosion, binary_erosion, binary_dilation
import numpy as np
import numpy.typing as npt
from pcgsepy.common.str_utils import get_matching_brackets
from pcgsepy.common.vecs import Orientation, Vec, orientation_from_vec
from pcgsepy.structure import Block, Structure, MountPoint
from typing import List, Optional, Tuple
from itertools import product
from pcgsepy.common.vecs import rotate, get_rotation_matrix
from enum import IntEnum


class BlockValue(IntEnum):
    AIR_BLOCK = 0
    BASE_BLOCK = 1
    SLOPE_BLOCK = 2
    CORNER_BLOCK = 3
    CORNERINV_BLOCK = 4
    CORNERSQUARE_BLOCK = 5
    CORNERSQUAREINV_BLOCK = 6


block_value_types = {
    BlockValue.BASE_BLOCK: 'MyObjectBuilder_CubeBlock_LargeBlockArmorBlock',
    BlockValue.SLOPE_BLOCK: 'MyObjectBuilder_CubeBlock_LargeBlockArmorSlope',
    BlockValue.CORNER_BLOCK: 'MyObjectBuilder_CubeBlock_LargeBlockArmorCorner',
    BlockValue.CORNERINV_BLOCK: 'MyObjectBuilder_CubeBlock_LargeBlockArmorCornerInv',
    BlockValue.CORNERSQUARE_BLOCK: 'MyObjectBuilder_CubeBlock_LargeBlockArmorCornerSquare',
    BlockValue.CORNERSQUAREINV_BLOCK: 'MyObjectBuilder_CubeBlock_LargeBlockArmorCornerSquareInverted',
}

_orientations = [Orientation.FORWARD, Orientation.BACKWARD, Orientation.UP, Orientation.DOWN, Orientation.LEFT, Orientation.RIGHT]
_valid_orientations = [(of, ou) for (of, ou) in list(product(_orientations, _orientations)) if of != ou and of != orientation_from_vec(ou.value.opposite())]
# _smoothing_order = {
#     BlockValue.BASE_BLOCK: [BlockValue.SLOPE_BLOCK, BlockValue.CORNERSQUARE_BLOCK, BlockValue.CORNER_BLOCK],
#     BlockValue.CORNERSQUAREINV_BLOCK: [],
#     BlockValue.CORNERINV_BLOCK: [],
#     BlockValue.SLOPE_BLOCK: [BlockValue.CORNERSQUARE_BLOCK, BlockValue.CORNER_BLOCK]
#     }
_smoothing_order = {
    BlockValue.BASE_BLOCK: [BlockValue.SLOPE_BLOCK],
    BlockValue.SLOPE_BLOCK: [BlockValue.CORNER_BLOCK]
    }


class HullBuilder:
    __slots__ = ['available_erosion_types', 'erosion_type', 'erosion', 'footprint', 'iterations', 'apply_erosion', 'apply_smoothing', 'base_block', 'obstruction_targets', '_blocks_set']
    
    def __init__(self,
                 erosion_type: str,
                 apply_erosion: bool,
                 apply_smoothing: bool):
        self.available_erosion_types = ['grey', 'bin']
        self.erosion_type = erosion_type
        assert self.erosion_type in self.available_erosion_types, f'Unrecognized erosion type {self.erosion_type}; available are {self.available_erosion_types}.'
        if self.erosion_type == 'grey':
            self.erosion = grey_erosion
            self.footprint=[
                [
                    [False, False, False],
                    [False, True, False],
                    [False, False, False]
                ],
                [
                    [False, True, False],
                    [True, True, True],
                    [False, True, False]
                ],
                [
                    [False, False, False],
                    [False, True, False],
                    [False, False, False]
                ]
            ]
        elif self.erosion_type == 'bin':
            self.erosion = binary_erosion
            self.iterations = 2
        self.apply_erosion = apply_erosion
        self.apply_smoothing = apply_smoothing
        
        self.base_block = 'MyObjectBuilder_CubeBlock_LargeBlockArmorBlock'
        self.obstruction_targets = ['window', 'thrust']
        self._blocks_set = {}
    
    def _get_convex_hull(self,
                         arr: np.ndarray) -> np.ndarray:
        """Compute the convex hull of the given array.

        Args:
            arr (np.ndarray): The Structure's array.

        Returns:
            np.ndarray: The convex hull.
        """
        points = np.transpose(np.where(arr))
        hull = ConvexHull(points)
        deln = Delaunay(points[hull.vertices])
        idx = np.stack(np.indices(arr.shape), axis=-1)
        out_idx = np.nonzero(deln.find_simplex(idx) + 1)
        out_arr = np.zeros(arr.shape)
        out_arr[out_idx] = BlockValue.BASE_BLOCK
        return out_arr
           
    def _add_block(self,
                   block_type: str,
                   idx: Tuple[int, int, int],
                   pos: Vec,
                   orientation_forward: Orientation = Orientation.FORWARD,
                   orientation_up: Orientation = Orientation.UP) -> None:
        """Add the block to the structure.

        Args:
            block_type (str): The block type.
            structure (Structure): The structure.
            pos (Tuple[int, int, int]): The grid coordinates (non-grid-size specific)
            orientation_forward (Orientation, optional): The forward orientation of the block. Defaults to Orientation.FORWARD.
            orientation_up (Orientation, optional): The up orientation of the block. Defaults to Orientation.UP.
        """
        block = Block(block_type=block_type,
                      orientation_forward=orientation_forward,
                      orientation_up=orientation_up)
        block.position = pos
        self._blocks_set[idx] = block
        
    def _exists_block(self,
                      idx: Tuple[int, int, int],
                      structure: Structure) -> bool:
        """Check if a block exists within the structure.

        Args:
            idx (Tuple[int, int, int]): The index to check at.
            structure (Structure): The structure.

        Returns:
            bool: Whether the block exists.
        """
        return structure._blocks.get(idx, None) is not None
    
    def _within_hull(self,
                     loc: Vec,
                     hull: npt.NDArray[np.float32]) -> bool:
        """Check if a block exists within the hull.

        Args:
            loc (Vec): The index to check at.
            hull (npt.NDArray[np.float32]): The hull array.

        Returns:
            bool: Whether the block is within the hull.
        """
        i, j, k = loc.as_tuple()
        return 0 <= i < hull.shape[0] and 0 <= j < hull.shape[1] and 0 <= k < hull.shape[2]
    
    def _is_air_block(self,
                      loc: Vec,
                      structure: Structure,
                      hull: np.typing.NDArray[np.float32]) -> bool:
        """Check if the block is an air block.

        Args:
            loc (Vec): The index to check at.
            structure (Structure): The structure.
            hull (np.typing.NDArray[np.float32]): The hull array.

        Returns:
            bool: Whether the block is an air block.
        """
        return not self._exists_block(idx=loc.as_tuple(), structure=structure) and\
            (self._within_hull(loc=loc.scale(1 / structure.grid_size).to_veci(), hull=hull) and hull[loc.scale(1 / structure.grid_size).to_veci().as_tuple()] == BlockValue.AIR_BLOCK)
    
    def _next_to_target(self,
                        loc: Vec,
                        structure: Structure,
                        direction: Vec) -> bool:
        """Check if the block is next to a target block along a direction.

        Args:
            loc (Vec): The index to check at.
            structure (Structure): The structure.
            direction (Vec): The direction to check at.

        Returns:
            bool: Whether the block is next to a target block.
        """
        dloc = loc.sum(direction)
        if self._exists_block(idx=dloc.as_tuple(), structure=structure):
            obs_block = structure._blocks.get(dloc.as_tuple())
            return any([target.lower() in obs_block.block_type.lower() for target in self.obstruction_targets])
    
    def _remove_in_direction(self,
                             loc: Vec,
                             hull: npt.NDArray[np.float32],
                             direction: Vec) -> npt.NDArray[np.float32]:
        """Remove all blocks in the hull along a direction.

        Args:
            loc (Vec): The starting index.
            hull (npt.NDArray[np.float32]): The hull array.
            direction (Vec): The direction to remove along to.

        Returns:
            npt.NDArray[np.float32]: The modified hull.
        """
        i, j, k = loc.as_tuple()
        di, dj, dk = direction.as_tuple()
        # TODO: improve if needed
        while 0 < i < hull.shape[0] and 0 < j < hull.shape[1] and 0 < k < hull.shape[2]:
            hull[i, j, k] = BlockValue.AIR_BLOCK
            i += di
            j += dj
            k += dk
            if (i, j, k) in self._blocks_set.keys():
                self._blocks_set.pop((i, j, k))
        return hull
    
    def _remove_obstructing_blocks(self,
                                   hull: npt.NDArray[np.float32],
                                   structure: Structure) -> npt.NDArray[np.float32]:
        """Remove the blocks obstructing a target block.

        Args:
            hull (npt.NDArray[np.float32]): The hull array.
            structure (Structure): The structure.

        Returns:
            npt.NDArray[np.float32]: The modified hull array.
        """
        scale = structure.grid_size
        for (i, j, k) in list(self._blocks_set.keys()):
            if hull[i, j, k] != BlockValue.AIR_BLOCK:  # skip removed blocks
                loc = Vec.from_tuple((scale * i, scale * j, scale * k))
                for direction in _orientations:
                    ntt = self._next_to_target(loc=loc,
                                               structure=structure,
                                               direction=direction.value.scale(scale))
                    if ntt:
                        hull[i, j, k] = BlockValue.AIR_BLOCK
                        self._blocks_set.pop((i, j, k))
                        hull = self._remove_in_direction(loc=loc.scale(v=1 / structure.grid_size).to_veci(),
                                                            hull=hull,
                                                            direction=direction.value.opposite())
                        break
        return hull
    
    def _remove_floating_blocks(self,
                                hull: npt.NDArray[np.float32],
                                structure: Structure,
                                pivot_blocktype: str = 'MyObjectBuilder_Cockpit_OpenCockpitLarge') -> npt.NDArray[np.float32]:
        """Remove floating blocks from the hull. Floating blocks are blocks not connected to the spaceship.
        These may appear when removing obstructing blocks.

        Args:
            hull (npt.NDArray[np.float32]): The hull array.
            structure (Structure): The structure.
            pivot_blocktype (str): The pivot block type. Defaults to 'MyObjectBuilder_Cockpit_OpenCockpitLarge'.

        Returns:
            npt.NDArray[np.float32]: The modified hull array.
        """
        structure_arr = structure.as_grid_array
        structure_shape = Vec.from_tuple(structure_arr.shape)
        # mask of all blocks
        mask = np.zeros_like(structure_arr, dtype=np.uint8)
        mask[np.nonzero(hull)] = BlockValue.BASE_BLOCK
        mask[np.nonzero(structure_arr)] = BlockValue.BASE_BLOCK
        # pivot position defines the region to keep
        pivot_position = [x for x in structure._blocks.values() if x.block_type == pivot_blocktype][0].position
        pivot_idx = pivot_position.scale(1 / structure.grid_size).to_veci().as_tuple()
        # get the region to keep defined by blocks connected to pivot position
        connected_blocks = set()
        to_check = [pivot_idx]
        while to_check:
            to_add = set()
            for block_position in to_check:
                if mask[block_position] == BlockValue.BASE_BLOCK:
                    connected_blocks.add(block_position)
                    for direction in _orientations:
                        dpos = Vec.from_tuple(block_position).sum(direction.value)
                        if 0 <= dpos.x < structure_shape.x and 0 <= dpos.y < structure_shape.y and 0 <= dpos.z < structure_shape.z:
                            to_add.add(dpos.as_tuple())
            to_add = to_add.difference(to_add.intersection(connected_blocks))
            to_check = list(to_add)
        # disconnected blocks are the difference between all blocks and blocks connected to pivot block
        disconnected_blocks = set(self._blocks_set.keys()) - connected_blocks
        # remove disconnected blocks
        for block_idx in disconnected_blocks:
            hull[block_idx] = BlockValue.AIR_BLOCK
            self._blocks_set.pop(block_idx)
        return hull
    
    def _get_outer_indices(self,
                           arr: npt.NDArray[np.float32],
                           edges_only: bool = False,
                           corners_only: bool = False) -> Tuple[npt.NDArray[np.int64], npt.NDArray[np.int64], npt.NDArray[np.int64]]:
        """Get the indices of blocks lying on the outer corner/edges/faces of the array.

        Args:
            arr (npt.NDArray[np.float32]): The array.
            edges_only (bool, optional): Get blocks on the edges only. Defaults to False.
            corners_only (bool, optional): Get blocks on the corners only. Defaults to False.

        Returns:
            Tuple[npt.NDArray[np.int64], npt.NDArray[np.int64], npt.NDArray[np.int64]]: The indices of the outer blocks.
        """
        n_neighbours = np.zeros_like(arr)
        for idx, _ in np.ndenumerate(arr):
            if self._blocks_set.get(idx, None):
                for offset in _orientations:
                    pos = Vec.from_tuple(idx).sum(offset.value).as_tuple()
                    if self._blocks_set.get(pos, None):
                        n_neighbours[idx] = n_neighbours[idx] + 1
        if corners_only:
            return np.nonzero(np.where(n_neighbours < 2, n_neighbours, 0))
        elif edges_only:
            ci, cj, ck = np.nonzero(np.where(n_neighbours < 2, n_neighbours, 0))
            ei, ej, ek = np.nonzero(np.where(n_neighbours <= 3, n_neighbours, 0))
            return np.append(ci, ei), np.append(cj, ej), np.append(ck, ek)
        else:
            return np.nonzero(n_neighbours)
    
    def adj_in_hull(self,
                    idx: Tuple[int, int, int]) -> List[Tuple[int, int, int]]:
        """Get the indices of adjacent blocks in the hull from a given index.

        Args:
            idx (Tuple[int, int, int]): The index.

        Returns:
            List[Tuple[int, int, int]]: The list of adjacent indices.
        """
        adjs = []
        for direction in _orientations:
            new_idx = Vec.from_tuple(idx).sum(direction.value).as_tuple()
            if new_idx in self._blocks_set.keys():
                adjs.append(new_idx)
        return adjs          
    
    def _get_neighbourhood(self,
                           idx: Vec,
                           structure: Structure) -> List[Block]:
        """Get the neighbourhood of a block.

        Args:
            idx (Vec): The index of the block.
            structure (Structure): The structure.

        Returns:
            List[Block]: The neighbourhood of the block.
        """
        n = []
        for di, dj, dk in zip([1, 1, 1, 1, 1,  1,  1,   0, 0, 0, 0,  0,  0,  -1, -1, -1, -1, -1, -1, -1],
                              [0, 1, 0, 1, -1, 0,  -1,  1, 0, 1, -1, 0,  -1, 0,  1,  0,  1,  -1, 0,  -1],
                              [0, 0, 1, 1, 0,  -1, -1,  0, 1, 1, 0,  -1, -1, 0,  0,  1,  1,  0,  -1, -1]):
            offset = Vec(di, dj, dk).scale(structure.grid_size)
            if not offset.is_zero:
                adj = self.try_and_get_block(idx=idx,
                                             offset=offset,
                                             structure=structure)
                if adj:
                    n.append(adj)
        return n
    
    def try_and_get_block(self,
                          idx: Tuple[int, int, int],
                          offset: Vec,
                          structure: Structure) -> Optional[Block]:
        """Try and get a block from either the hull or the structure.

        Args:
            idx (Tuple[int, int, int]): The index of the block (hull-scaled).
            offset (Vec): The offset vector (structure-scaled).
            structure (Structure): The structure.

        Returns:
            Optional[Block]: The block at `idx + offset`, if it exists.
        """
        dloc = Vec.from_tuple(idx).scale(structure.grid_size).sum(offset)
        if self._exists_block(idx=dloc.as_tuple(),
                              structure=structure):
            return structure._blocks[dloc.as_tuple()]
        else:
            return self._blocks_set.get(Vec.from_tuple(idx).sum(offset.scale(1 / structure.grid_size)).to_veci().as_tuple(), None)
    
    def _correct_and_centered_rotation(self,
                                       center: Vec,
                                       rotation_matrix: np.typing.NDArray,
                                       vector: Vec) -> Vec:
        """Rotate correctly and center the vector to the center of the block.

        Args:
            center (Vec): The center of the block.
            rotation_matrix (np.typing.NDArray): The rotation matrix of the block.
            vector (Vec): The vector to rotate and center.

        Returns:
            Vec: The rotated and centered vector.
        """
        # Rotation with additional checking as in Space Engineers source code.
        centered_vector = vector.diff(center)
        centered_vector_i = centered_vector.floor()
        rotated_vector = rotate(rotation_matrix=rotation_matrix,
                                vector=centered_vector)
        rotated_vector_i_correct = rotate(rotation_matrix=rotation_matrix,
                                          vector=centered_vector_i)
        rotated_vector_i = rotated_vector.floor()
        correction = rotated_vector_i_correct.diff(rotated_vector_i)
        return rotated_vector.sum(correction)
    
    def _get_mountpoint_limits(self,
                               mountpoints: List[MountPoint],
                               block_center: Vec,
                               rotation_matrix: npt.NDArray[np.float32]) -> Tuple[List[Vec], List[Vec], List[int]]:
        """Get the start and end vectors of the mountpoint, as well as the face area(s).

        Args:
            mountpoints (List[MountPoint]): The list of mountpoints.
            block_center (Vec): The center of the block.
            rotation_matrix (npt.NDArray[np.float32]): The rotation matrix of the block.

        Returns:
            Tuple[List[Vec], List[Vec], List[int]]: The start and end vector, and the face area(s).
        """
        starts, ends = [], []
        for mp in mountpoints:
            rotated_start = self._correct_and_centered_rotation(center=block_center,
                                                                rotation_matrix=rotation_matrix,
                                                                vector=mp.start)
            rotated_end = self._correct_and_centered_rotation(center=block_center,
                                                              rotation_matrix=rotation_matrix,
                                                              vector=mp.end)

            ordered_start = Vec.min(rotated_start, rotated_end)
            ordered_end = Vec.max(rotated_start, rotated_end)
            
            starts.append(ordered_start)
            ends.append(ordered_end)
        
        planes = [end.diff(start).bbox(ignore_zero=False) for start, end in zip(starts, ends)]
        
        return starts, ends, planes
    
    def intersect_planes(self,
                         starts1: List[Vec],
                         ends1: List[Vec],
                         starts2: List[Vec],
                         ends2: List[Vec]) -> float:
        """Compute the intersection surface between mountpoints of different blocks and return the inverse of the intersection as an error to minimize.

        Args:
            starts1 (List[Vec]): The start vectors of the first block.
            ends1 (List[Vec]): The end vectors of the first block.
            starts2 (List[Vec]): The start vectors of the second block.
            ends2 (List[Vec]): The end vectors of the first block.

        Returns:
            float: The intersection as an error.
        """
        intersect_bbox = 0.
        for start1, end1 in zip(starts1, ends1):
            for start2, end2 in zip(starts2, ends2):
                x1 = max(start1.x, start2.x)
                y1 = max(start1.y, start2.y)
                z1 = max(start1.z, start2.z)
                x2 = min(end1.x, end2.x)
                y2 = min(end1.y, end2.y)
                z2 = min(end1.z, end2.z)
                if x2 > x1 and y2 > y1 and z2 < z1:
                    intersect_bbox += (x2 - x1) * (y2 - y1)
                elif x2 > x1 and y2 <= y1 and z2 > z1:
                    intersect_bbox += (x2 - x1) * (z2 - z1)
                elif x2 <= x1 and y2 > y1 and z2 > z1:
                    intersect_bbox += (y2 - y1) * (z2 - z1)
                else:
                    intersect_bbox += 0
        return 1 / intersect_bbox
    
    def _check_valid_placement(self,
                               idx: Tuple[int, int, int],
                               block: Block,
                               direction: Orientation,
                               structure: Structure) -> Tuple[bool, int]:
        """Check if the block could be placed with the given orientation when checking in the specified direction.

        Args:
            idx (Tuple[int, int, int]): The index of the block (hull-scaled).
            block (Block): The block to be checked.
            direction (Orientation): The direction to check placement for.
            structure (Structure): The structure.

        Returns:
            bool: Whether the block could be placed with the given orientation when checking in the specified direction.
        """
        rot_mat = get_rotation_matrix(forward=block.orientation_forward,
                                      up=block.orientation_up)
        mp1 = [mp for mp in block.mountpoints if rotate(rot_mat, mp.face) == direction.value]
        starts1, ends1, planes1 = self._get_mountpoint_limits(mountpoints=mp1,
                                                              block_center=block.center,
                                                              rotation_matrix=rot_mat)
        other_block = self.try_and_get_block(idx=idx,
                                             offset=direction.value.scale(structure.grid_size).to_veci(),
                                             structure=structure)
        opposite_direction = orientation_from_vec(direction.value.opposite())
        if other_block is None:
            if mp1 == []:
                mp2 = [mp for mp in block.mountpoints if rotate(rot_mat, mp.face) == opposite_direction.value]
                _, _, planes2 = self._get_mountpoint_limits(mountpoints=mp2,
                                                            block_center=block.center,
                                                            rotation_matrix=rot_mat)
                val = sum(planes2)  # error as the surface of mountpoints on opposite face
            else:
                val = sum(planes1)  # erorr as surface of mountpoints of current face
            return True, val
        else:
            if mp1 == []:
                return False, 0
            else:
                rot_mat_other = get_rotation_matrix(forward=other_block.orientation_forward,
                                                    up=other_block.orientation_up)
                mp2 = [mp for mp in other_block.mountpoints if rotate(rot_mat_other, mp.face) == opposite_direction.value]
                if mp2 == []:
                    return False, 0
                starts2, ends2, planes2 = self._get_mountpoint_limits(mountpoints=mp2,
                                                                      block_center=other_block.center,
                                                                      rotation_matrix=rot_mat_other)
                all_valid = []
                for eo1, so1, p1 in zip(ends1, starts1, planes1):
                    assert p1 != 0, f'Mountpoint with empty surface: {mp1} has {so1}-{eo1} (from block {block})'
                    mp_valid = True
                    for eo2, so2, p2 in zip(ends2, starts2, planes2):
                        assert p2 != 0, f'Mountpoint with empty surface: {mp2} has {so2}-{eo2} (from block {other_block})'
                        # NOTE: This check does not take into account exclusions and properties masks (yet)
                        # Sources\VRage.Math\BoundingBoxI.cs#328
                        # (double)this.Max.X >= (double)box.Min.X && (double)this.Min.X <= (double)box.Max.X && ((double)this.Max.Y >= (double)box.Min.Y && (double)this.Min.Y <= (double)box.Max.Y) && ((double)this.Max.Z >= (double)box.Min.Z && (double)this.Min.Z <= (double)box.Max.Z);
                        mp_valid |= eo1.x >= so2.x and so1.x <= eo2.x and eo1.y >= so2.y and so1.y <= eo2.y and eo1.z >= so2.z and so1.z <= eo2.z
                    all_valid.append(mp_valid)
                return all(all_valid), self.intersect_planes(starts1, ends1, starts2, ends2)
        
    def _check_valid_position(self,
                              idx: Tuple[int, int, int],
                              block: Block,
                              hull: np.typing.NDArray,
                              structure: Structure) -> Tuple[bool, int]:
        """Check if the current position is valid for the given block.

        Args:
            idx (Tuple[int, int, int]): The index of the block.
            block (Block): The block to check.
            hull (npt.NDArray[np.float32]): The hull array.
            structure (Structure): The structure.

        Returns:
            Tuple[bool, int]: Whether the position is valid, and the area error.
        """
        valid = True
        area_err = 0
        for direction in _orientations:
            res, delta_area = self._check_valid_placement(idx=idx,
                                                          block=block,
                                                          direction=direction,
                                                          structure=structure)
            valid &= res
            area_err += delta_area
            if not valid:
                break
        return valid, delta_area
    
    def try_smoothing(self,
                      idx: Tuple[int, int, int],
                      hull: npt.NDArray[np.float32],
                      structure: Structure) -> Optional[Block]:
        """Try applying a smoothing pass at the given index.

        Args:
            idx (Tuple[int, int, int]): The index.
            hull (npt.NDArray[np.float32]): The hull array.
            structure (Structure): The structure.

        Returns:
            Optional[Block]: A possible block to replace the current one.
        """
        i, j, k = idx
        block_type = hull[i, j, k]
        block = self._blocks_set[idx]
        # removal check
        valid, curr_err = self._check_valid_position(idx=idx,
                                                     block=block,
                                                     hull=hull,
                                                     structure=structure)
        if not valid:
            return None, BlockValue.AIR_BLOCK
        # replacement check   
        elif block_type in _smoothing_order.keys():
            # give priority to surrounding blocks orientations
            neighbourhood = self._get_neighbourhood(idx=idx,
                                                    structure=structure)
            priority_scores = np.zeros(shape=len(_valid_orientations), dtype=np.int8).tolist()
            for other_block in neighbourhood:                
                oo = (orientation_from_vec(other_block.orientation_forward),
                        orientation_from_vec(other_block.orientation_up))
                priority_scores[_valid_orientations.index(oo)] = priority_scores[_valid_orientations.index(oo)] + (1 if other_block.block_type == block_type else 0)
            idxs = [x for _, x in sorted(zip(priority_scores, np.arange(len(_valid_orientations)).tolist()))]
            priority_orientations = [_valid_orientations[i] for i in idxs]
            for possible_type in _smoothing_order[block_type]:
                orientation_scores, valids = np.zeros(shape=len(_valid_orientations), dtype=np.float32), np.zeros(shape=len(_valid_orientations), dtype=np.bool8)
                # try replacement
                for i, (of, ou) in enumerate(priority_orientations):
                    possible_block = Block(block_type=block_value_types[possible_type],
                                           orientation_forward=of,
                                           orientation_up=ou)
                    valid, err = self._check_valid_position(idx=idx,
                                                            block=possible_block,
                                                            hull=hull,
                                                            structure=structure)
                    orientation_scores[i] = err if valid else 9999  # make sure invalid scores are never picked
                    valids[i] = valid
                if any(valids) and min(orientation_scores) < curr_err:
                    of, ou = priority_orientations[np.argmin(orientation_scores)]
                    return Block(block_type=block_value_types[possible_type],
                                orientation_forward=of,
                                orientation_up=ou), possible_type
            return None, block_type
        # skip
        else:
            return None, block_type
        
    def add_external_hull(self,
                          structure: Structure) -> None:
        """Add an external hull to the given Structure.
        This process adds the hull blocks directly into the Structure, so it can be used only once per spaceship.

        Args:
            structure (Structure): The spaceship.
        """
        self._blocks_set = {}
        
        arr = structure.as_grid_array
        air = structure.air_blocks_gridmask
        hull = self._get_convex_hull(arr=arr)
        hull[np.nonzero(air)] = BlockValue.AIR_BLOCK
        hull[np.nonzero(arr)] = BlockValue.AIR_BLOCK
        logging.getLogger('hullbuilder').debug(f'[{__name__}.add_external_hull] Added external hull.')
        
        if self.apply_erosion:
            if self.erosion_type == 'grey':
                hull = grey_erosion(input=hull,
                                    footprint=self.footprint,
                                    mode='constant',
                                    cval=1)
                hull = hull.astype(int)
                hull *= BlockValue.BASE_BLOCK                
            elif self.erosion_type == 'bin':
                dilated_arr = binary_dilation(arr)
                mask = np.ones_like(arr) * BlockValue.BASE_BLOCK
                mask[np.nonzero(dilated_arr)] = BlockValue.AIR_BLOCK                
                hull = binary_erosion(input=hull,
                                      mask=mask,
                                      iterations=self.iterations)
                hull = hull.astype(int)
                hull *= BlockValue.BASE_BLOCK
        logging.getLogger('hullbuilder').debug(f'[{__name__}.add_external_hull] Applied erosion.')
                
        # add blocks to self._blocks_set
        for (i, j, k), _ in np.ndenumerate(hull):
            if hull[i, j, k] != BlockValue.AIR_BLOCK:
                self._add_block(block_type=self.base_block,
                                idx=(i, j, k),
                                pos=Vec.v3i(i, j, k).scale(v=structure.grid_size),
                                orientation_forward=Orientation.FORWARD,
                                orientation_up=Orientation.UP)
        
        # remove all blocks that obstruct target block type
        hull = self._remove_obstructing_blocks(hull=hull,
                                            structure=structure)
        logging.getLogger('hullbuilder').debug(f'[{__name__}.add_external_hull] Removed obstructing blocks.')
        
        # remove "floating" blocks
        # i.e.: blocks not connected to the spaceship
        hull = self._remove_floating_blocks(hull=hull,
                                            structure=structure)
        logging.getLogger('hullbuilder').debug(f'[{__name__}.add_external_hull] Removed non-connected blocks.')

        # replace structure's blocks if adjacent to hull
        for idx in self._blocks_set.keys():
            for direction in _orientations:
                new_idx = Vec.from_tuple(idx).sum(direction.value).scale(structure.grid_size).as_tuple()
                if new_idx in structure._blocks.keys():
                    curr_block = structure._blocks[new_idx]
                    structure._blocks[new_idx] = Block(block_type=block_value_types[BlockValue.BASE_BLOCK],
                                                       orientation_forward=orientation_from_vec(curr_block.orientation_forward),
                                                       orientation_up=orientation_from_vec(curr_block.orientation_up))
        logging.getLogger('hullbuilder').debug(f'[{__name__}.add_external_hull] Replaced existing adjacent structure blocks.')
        
        # apply iterative smoothing algorithm
        if self.apply_smoothing:
            logging.getLogger('hullbuilder').debug(f'[{__name__}.add_external_hull] Applying smoothing...')
            my_arr = np.ones_like(hull) * BlockValue.AIR_BLOCK
            my_arr[np.nonzero(structure.as_grid_array)] = BlockValue.BASE_BLOCK
            my_arr[np.nonzero(hull)] = BlockValue.BASE_BLOCK
            within_arr = binary_erosion(my_arr)
            my_arr[np.where(within_arr == BlockValue.AIR_BLOCK)] = BlockValue.AIR_BLOCK
            # idxs = self._get_outer_indices(arr=my_arr, edges_only=True)
            idxs = self._get_outer_indices(arr=my_arr)
            ii, jj, kk = idxs
            curr_checking = [(i, j, k) for (i, j, k) in zip(ii, jj, kk)]
            while len(curr_checking) != 0:
                to_rem, to_inspect = [], []
                for (i, j, k) in curr_checking:
                    block = self._blocks_set[(i, j, k)]
                    substitute_block, val = self.try_smoothing(idx=(i, j, k),
                                                               hull=hull,
                                                               structure=structure)
                    if substitute_block is not None and substitute_block.block_type != block.block_type:
                        substitute_block.position = block.position
                        self._blocks_set[(i, j, k)] = substitute_block
                        to_inspect.extend(self.adj_in_hull(idx=(i, j, k)))
                        logging.getLogger('hullbuilder').debug(f'[{__name__}.add_external_hull] Updated {substitute_block}.')
                    elif substitute_block is None and val == BlockValue.AIR_BLOCK:
                        to_rem.append((i, j, k))
                        to_inspect.extend(self.adj_in_hull(idx=(i, j, k)))
                    hull[i, j, k] = val
                to_inspect = list(set(to_inspect))
                to_rem = list(set(to_rem))
                for r in to_rem:
                    self._blocks_set.pop(r)
                    if r in to_inspect:
                        to_inspect.remove(r)
                    logging.getLogger('hullbuilder').debug(f'[{__name__}.add_external_hull] Removed {r}.')
                curr_checking = to_inspect
        logging.getLogger('hullbuilder').debug(f'[{__name__}.add_external_hull] Smoothing applied.')

        # add blocks to structure
        for k, block in self._blocks_set.items():
            structure.add_block(block=block,
                                grid_position=block.position.as_tuple())
        
        structure.sanify()


def enforce_symmetry(string: str,
                     axis: str = 'z') -> str:
    """Enforce a symmetry along an axis.

    Args:
        string (str): The high-level string.
        axis (str, optional): The axis to enforce symmetry along to. Defaults to 'z'.
    """
    def __inverse(rot: str) -> str:
        if rot.count('c') == 1:
            return f"{rot[:rot.index('c')]}c{rot[rot.index('c'):]}"
        elif rot.count('c') == 2:
            return rot.replace('c', '', 1)
    
    brackets = get_matching_brackets(string=string)
    symm_points, to_remove = [], []
    logging.getLogger('hullbuilder').debug(f'[{__name__}.enforce_symmetry] {string=}; {brackets=}')
    
    if axis == 'x':
        rotations = ['RotYcwX', 'RotYccwX']
    elif axis == 'z':
        rotations = ['RotYcwZ', 'RotYccwZ']
    else:
        raise ValueError(f'Invalid rotation axis: {axis}.')
    
    for b0, b1 in brackets:
        for rotation in rotations:
            if string[b0 + 1:b0 + 1 + len(rotation)].startswith(rotation) or string[b0 + 1:b0 + 1 + len(rotation)].startswith(rotation):
                symm_points.append(((b0, b1), rotation))
    logging.getLogger('hullbuilder').debug(f'[{__name__}.enforce_symmetry] (pre) {symm_points=}')
    
    for i, ((b00, b01), rotation0) in enumerate(symm_points):
        for j, ((b10, b11), rotation1) in enumerate(symm_points[i:]):
            if j + i not in to_remove:
                logging.getLogger('hullbuilder').debug(f'[{__name__}.enforce_symmetry] {((b00, b01), rotation0)}-{((b10, b11), rotation1)}; {b10 == b01 + 1=}; {rotation0 == __inverse(rotation1)=}; {b00 < b10 < b11 < b01=}')
                if b10 == b01 + 1 and rotation0 == __inverse(rotation1):
                    to_remove.append(i)
                    to_remove.append(j + i)
                    break
                if b00 < b10 < b11 < b01:
                    to_remove.append(j + i)
    logging.getLogger('hullbuilder').debug(f'[{__name__}.enforce_symmetry] {to_remove=}')
    
    for i in reversed(sorted(to_remove)):
        symm_points.pop(i)
    logging.getLogger('hullbuilder').debug(f'[{__name__}.enforce_symmetry] (post) {symm_points=}')
    
    for (b0, b1), rotation in reversed(symm_points):
        substring = string[b0:b1+1]
        substring = substring.replace(rotation, __inverse(rotation))
        string = string[:b1+1] + substring + string[b1+1:]
    logging.getLogger('hullbuilder').debug(f'[{__name__}.enforce_symmetry] (post) {string=}')
    
    return string
    