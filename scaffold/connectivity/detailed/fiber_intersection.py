import numpy as np
import math
from ..strategy import ConnectionStrategy
from .shared import MorphologyStrategy
from ...models import MorphologySet
from ...exceptions import *
from ...helpers import ConfigurableClass
from ...networks import FiberMorphology, Branch
import abc

# Import rtree
from rtree import index
from rtree.index import Rtree


class FiberIntersection(ConnectionStrategy, MorphologyStrategy):
    """
        FiberIntersection connection strategies voxelize a fiber and find its intersections with postsynaptic cells.
        It's a specific case of VoxelIntersection.

        For each presynaptic cell, the following steps are executed:

        #. Extract the FiberMorphology
        #. Interpolate points on the fiber until the spatial resolution is respected
        #. transform
        #. Interpolate points on the fiber until the spatial resolution is respected
        #. Voxelize (generates the voxel_tree associated to this morphology)
        #. Check intersections of presyn bounding box with all postsyn boxes
        #. Check intersections of each candidate postsyn with current presyn voxel_tree

    """

    casts = {
        "affinity": float,
        "resolution": float,
    }

    defaults = {"affinity": 1.0, "resolution": 20.0}

    def validate(self):
        pass

    def connect(self):
        scaffold = self.scaffold

        p = index.Property(dimension=3)
        to_cell_tree = index.Index(properties=p)

        # Select all the cells from the pre- & postsynaptic type for a specific connection.
        from_type = self.from_cell_types[0]
        from_compartments = self.from_cell_compartments[0]
        to_compartments = self.to_cell_compartments[0]
        to_type = self.to_cell_types[0]
        from_placement_set = self.scaffold.get_placement_set(from_type.name)
        to_placement_set = self.scaffold.get_placement_set(to_type.name)

        # Load the morphology and voxelization data for the entrire morphology, for each cell type.
        from_morphology_set = MorphologySet(
            scaffold, from_type, from_placement_set, compartment_types=from_compartments
        )

        to_morphology_set = MorphologySet(
            scaffold, to_type, to_placement_set, compartment_types=to_compartments
        )
        joined_map = (
            from_morphology_set._morphology_map + to_morphology_set._morphology_map
        )
        joined_map_offset = len(from_morphology_set._morphology_map)

        # For every postsynaptic cell, derive the box incorporating all voxels,
        # and store that box in the tree, to later find intersections with that cell.
        for i, (to_cell, morphology) in enumerate(to_morphology_set):
            self.assert_voxelization(morphology, to_compartments)
            to_offset = np.concatenate((to_cell.position, to_cell.position))
            to_box = morphology.cloud.get_voxel_box()
            to_cell_tree.insert(i, tuple(to_box + to_offset))

        connections_out = []
        compartments_out = []
        morphologies_out = []

        for c, (from_cell, from_morpho) in enumerate(from_morphology_set):
            # (1) Extract the FiberMorpho object for each branch in the from_compartments
            # of the presynaptic morphology
            compartments = from_morpho.get_compartments(
                compartment_types=from_compartments
            )
            morpho_rotation = from_cell.rotation
            fm = FiberMorphology(compartments, morpho_rotation)

            # (2) Interpolate all branches recursively
            self.interpolate_branches(fm.root_branches)

            # (3) Transform the fiber if present
            if self.transformation is not None:
                self.transformation.transform_branches(
                    fm.root_branches, from_cell.position
                )

            # (4) Interpolate again
            self.interpolate_branches(fm.root_branches)
            #

            # (5) Voxelize all branches of the transformed fiber morphology
            p = index.Property(dimension=3)
            # The bounding box is incrementally expanded, these initial bounds are a point
            # at the start of the first root branch.
            from_bounding_box = [
                fm.root_branches[0]._compartments[0].start + from_cell.position
            ] * 2
            from_voxel_tree = index.Index(properties=p)
            from_map = []
            from_bounding_box, from_voxel_tree, from_map, v_all = self.voxelize_branches(
                fm.root_branches,
                from_cell.position,
                from_bounding_box,
                from_voxel_tree,
                from_map,
            )

            # (6) Check for intersections of the postsyn tree with the bounding box

            ## TODO: Check if bounding box intersection is convenient

            # Bounding box intersection to identify possible connected candidates, using
            # the bounding box of the point cloud. Query the Rtree for intersections of
            # to_cell boxes with our from_cell box
            cell_intersections = list(
                to_cell_tree.intersection(
                    tuple(np.concatenate(from_bounding_box)), objects=False
                )
            )

            # (7) For each hit on the box intersection between pre- and postsynaptic
            # cells, perform voxel cloud intersection to identify actually connected cell
            # pairs and select compartments from their intersecting voxels to form
            # connections with.
            for partner in cell_intersections:
                # Same as in VoxelIntersection, only select a fraction of the total
                # possible matches, based on how much affinity there is between the cell
                # types.
                if np.random.rand() >= self.affinity:
                    continue
                # Get the precise morphology of the to_cell we collided with
                to_cell, to_morpho = to_morphology_set[partner]
                # Get the map from voxel id to list of compartments in that voxel.
                to_map = to_morpho.cloud.map
                # Find which voxels inside the bounding box of the fiber and the cell box
                # actually intersect with eachother.
                voxel_intersections = self.intersect_voxel_tree(
                    from_voxel_tree, to_morpho.cloud, to_cell.position
                )
                # Returns a list of lists: the elements in the inner lists are the indices
                # of the voxels in the from point cloud, the indices of the lists inside
                # of the outer list are the to voxel indices.
                #
                # Find non-empty lists: these voxels actually have intersections
                intersecting_to_voxels = np.nonzero(voxel_intersections)[0]
                if not len(intersecting_to_voxels):
                    # No intersections found? Do nothing, continue to next partner.
                    continue
                # Dictionary that stores the target compartments for each to_voxel.
                target_comps_per_to_voxel = {}

                # Iterate over each to_voxel index.
                for to_voxel_id in intersecting_to_voxels:
                    # Get the list of voxels that the to_voxel intersects with.
                    intersecting_voxels = voxel_intersections[to_voxel_id]
                    target_compartments = []

                    for from_voxel_id in intersecting_voxels:
                        # Store all of the compartments in the from_voxel as
                        # possible candidates for these cells' connections
                        target_compartments.extend([from_map[from_voxel_id]])
                    target_comps_per_to_voxel[to_voxel_id] = target_compartments
                # Weigh the random sampling by the amount of compartments so that voxels
                # with more compartments have a higher chance of having one of their many
                # compartments randomly picked.
                voxel_weights = [
                    len(to_map[to_voxel_id]) * len(from_targets)
                    for to_voxel_id, from_targets in target_comps_per_to_voxel.items()
                ]
                weight_sum = sum(voxel_weights)
                voxel_weights = [w / weight_sum for w in voxel_weights]
                # Pick a random voxel and its targets
                candidates = list(target_comps_per_to_voxel.items())
                random_candidate_id = np.random.choice(
                    range(len(candidates)), 1, p=voxel_weights
                )[0]
                # Pick a to_voxel_id and its target compartments from the list of candidates
                random_to_voxel_id, random_compartments = candidates[random_candidate_id]
                # Pick a random from and to compartment of the chosen voxel pair
                from_compartment = np.random.choice(random_compartments, 1)[0]
                to_compartment = np.random.choice(to_map[random_to_voxel_id], 1)[0]
                compartments_out.append([from_compartment.id, to_compartment])
                morphologies_out.append(
                    [from_morpho._set_index, joined_map_offset + to_morpho._set_index]
                )
                connections_out.append([from_cell.id, to_cell.id])

        self.scaffold.connect_cells(
            self,
            np.array(connections_out or np.empty((0, 2))),
            morphologies=np.array(morphologies_out or np.empty((0, 2), dtype=str)),
            compartments=np.array(compartments_out or np.empty((0, 2))),
            morpho_map=joined_map,
        )

    def intersect_voxel_tree(self, from_voxel_tree, to_cloud, to_pos):
        """
            Similarly to `intersect_clouds` from `VoxelIntersection`, it finds intersecting voxels between a from_voxel_tree
            and a to_cloud set of voxels

            :param from_voxel_tree: tree built from the voxelization of all branches in the fiber (in absolute coordinates)
            :type from_point_cloud: Rtree index
            :param to_cloud: voxel cloud associated to a to_cell morphology
            :type to_cloud: `VoxelCloud`
            :param to_pos: 3-D position of to_cell neuron
            :type to_pos: list
        """

        voxel_intersections = []

        # Find intersection of to_cloud with from_voxel_tree
        for v, voxel in enumerate(to_cloud.get_voxels(cache=True)):
            absolute_position = np.add(voxel, to_pos)
            absolute_box = np.add(absolute_position, to_cloud.grid_size)
            box = np.concatenate((absolute_position, absolute_box))
            voxel_intersections.append(
                list(from_voxel_tree.intersection(tuple(box), objects=False))
            )
        return voxel_intersections

    def assert_voxelization(self, morphology, compartment_types):
        if len(morphology.cloud.get_voxels()) == 0:
            raise IncompleteMorphologyError(
                "Can't intersect without any {} in the {} morphology".format(
                    ", ".join(compartment_types), morphology.morphology_name
                )
            )

    def interpolate_branches(self, branches):
        for branch in branches:
            branch.interpolate(self.resolution)
            self.interpolate_branches(branch.child_branches)

    def voxelize_branches(
        self, branches, position, bounding_box=None, voxel_tree=None, map=None
    ):
        v = 0
        for branch in branches:
            bounding_box, voxel_tree, map, v = branch.voxelize(
                position, bounding_box, voxel_tree, map
            )

            self.voxelize_branches(
                branch.child_branches, position, bounding_box, voxel_tree, map
            )

        return bounding_box, voxel_tree, map, v


class FiberTransform(ConfigurableClass):
    def transform_branches(self, branches, offset=None):
        if offset is None:
            offset = np.zeros(3)
        for branch in branches:
            self.transform_branch(branch, offset)
            self.transform_branches(branch.child_branches, offset)

    @abc.abstractmethod
    def transform_branch(self):
        pass


class QuiverTransform(FiberTransform):
    """
        QuiverTransform applies transformation to a FiberMorphology, based on an orientation field in a voxelized volume.
        Used for parallel fibers.
    """

    # Class attributes

    casts = {"vol_res": float}

    defaults = {"vol_res": 1.0, "quivers": [1.0, 1.0, 1.0]}

    def validate(self):
        raise NotImplementedError("QuiverTransform not implemented")

    def transform_branch(self, branch, offset):
        raise NotImplementedError("QuiverTransform not implemented")
