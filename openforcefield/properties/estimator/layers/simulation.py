# =============================================================================================
# MODULE DOCSTRING
# =============================================================================================

"""
Direct simulation layer.

Authors
-------
* Simon Boothroyd <simon.boothroyd@choderalab.org>

.. todo:: * Make all protocol execute methods static.
          * Create a pydantic data model to pass input / outputs between protocols.
"""


# =============================================================================================
# GLOBAL IMPORTS
# =============================================================================================

import copy
import logging
import uuid

import os
from os import path

from openforcefield.utils import graph

from openforcefield.properties import PhysicalProperty, CalculationSource
from openforcefield.properties.estimator import CalculationSchema
from openforcefield.properties.estimator.components import protocols
from openforcefield.properties.estimator.layers.base import register_calculation_layer, PropertyCalculationLayer


# =============================================================================================
# Direct Calculation Classes
# =============================================================================================

class DirectCalculation:
    """Defines the property to calculate and the calculation
    workflow needed to calculate it.
    """

    def __init__(self, physical_property, force_field_path, schema):
        """
        Constructs a new DirectCalculation object.

        Parameters
        ----------
        physical_property: PhysicalProperty
            The protocol this node will execute.
        force_field_path: str
            The force field to use for this calculation.
        schema: CalculationSchema
            The schema to use to calculate this property.
        """
        self.physical_property = physical_property

        self.physical_property.source = CalculationSource(fidelity=SimulationLayer.__name__,
                                                          provenance=schema.json())

        self.uuid = str(uuid.uuid4())

        self.protocols = {}

        self.starting_protocols = []
        self.dependants_graph = {}

        self.final_value_reference = None
        self.final_uncertainty_reference = None

        schema = CalculationSchema.parse_raw(schema.json())

        # Define a dictionary of accessible 'global' properties.
        self.global_properties = {
            "thermodynamic_state": physical_property.thermodynamic_state,
            "substance": physical_property.substance,
            "uncertainty": physical_property.uncertainty,
            "force_field_path": force_field_path
        }

        # A helper to map the template protocol ids to
        # the new, uuid appended ones.
        id_map = {}

        for protocol_name in schema.protocols:

            protocol_schema = schema.protocols[protocol_name]

            protocol = protocols.available_protocols[protocol_schema.type]()
            protocol.schema = protocol_schema

            # Try to set global properties on each of the protocols
            protocol.set_global_properties(self.global_properties)

            protocol.set_uuid(self.uuid)
            id_map[protocol_name] = protocol.id

            self.protocols[protocol.id] = protocol

        self._build_dependants_graph()
        self._apply_groups(schema)

        self.final_value_reference = copy.deepcopy(schema.final_value_reference)
        self.final_value_reference.set_uuid(self.uuid)

        self.final_uncertainty_reference = copy.deepcopy(schema.final_uncertainty_reference)
        self.final_uncertainty_reference.set_uuid(self.uuid)

    def _build_dependants_graph(self):
        """Builds a dictionary of key value pairs where each key represents the id of a
        protocol to be executed in this calculation, and each value a list ids of protocols
        which must be ran after the protocol identified by the key.
        """

        for protocol_name in self.protocols:
            self.dependants_graph[protocol_name] = []

        for dependant_protocol_name in self.protocols:

            dependant_protocol = self.protocols[dependant_protocol_name]

            for input_reference in dependant_protocol.input_references:

                if input_reference.output_protocol_id == 'global':
                    # Global inputs are outside the scope of the
                    # schema dependency graph.
                    continue

                if dependant_protocol.id in self.dependants_graph[input_reference.output_protocol_id]:
                    continue

                self.dependants_graph[input_reference.output_protocol_id].append(dependant_protocol.id)

        self.starting_protocols = graph.find_root_nodes(self.dependants_graph)

    def _apply_groups(self, schema):
        """Groups protocols together into a set of user defined groups."""

        if len(schema.groups) == 0:
            # Nothing to do here.
            return

        # TODO: Implement groups.
        return
        #
        # for group_id in self.groups:
        #
        #     group = self.groups[group_id]
        #
        #     # Remove all grouped protocols from the protocol list.
        #     for grouped_protocol_id in group.protocols:
        #         self.protocols.pop(grouped_protocol_id)
        #
        #     # Point the other protocols to the groups rather than
        #     # the removed protocols
        #     for grouped_protocol_id in group.protocols:
        #
        #         for protocol_id in self.protocols:
        #
        #             protocol = self.protocols[protocol_id]
        #             protocol.replace_protocol(grouped_protocol_id, group.id)
        #
        #             for input_reference in protocol.input_references:
        #
        #                 if input_reference.output_protocol_id != group.id:
        #                     continue
        #
        #                 input_reference.grouped_protocol_id = grouped_protocol_id
        #
        #         if self.final_value_reference.output_protocol_id == grouped_protocol_id:
        #             self.final_value_reference.output_protocol_id = group.id
        #             self.final_value_reference.grouped_protocol_id = grouped_protocol_id
        #
        #         if self.final_uncertainty_reference.output_protocol_id == grouped_protocol_id:
        #             self.final_uncertainty_reference.output_protocol_id = group.id
        #             self.final_uncertainty_reference.grouped_protocol_id = grouped_protocol_id
        #
        #     # Add the group in their place.
        #     self.protocols[group.id] = group

    def replace_protocol(self, old_protocol, new_protocol):
        """Replaces an existing protocol with a new one, while
        updating all input and local references to point to the
        new protocol.

        The main use of this method is when merging multiple protocols
        into one.

        Parameters
        ----------
        old_protocol : protocols.BaseProtocol
            The protocol to replace.
        new_protocol : protocols.BaseProtocol
            The new protocol to use.
        """

        if new_protocol.id in self.protocols:
            raise ValueError('A protocol with the same id already exists in this calculation.')

        for protocol_id in self.protocols:

            protocol = self.protocols[protocol_id]
            protocol.replace_protocol(old_protocol.id, new_protocol.id)

        if old_protocol.id in self.protocols:

            self.protocols.pop(old_protocol.id)
            self.protocols[new_protocol] = new_protocol

        for index, starting_id in enumerate(self.starting_protocols):

            if starting_id == old_protocol.id:
                starting_id = new_protocol.id

            self.starting_protocols[index] = starting_id

        for protocol_id in self.dependants_graph:

            for index, dependant_id in enumerate(self.dependants_graph[protocol_id]):

                if dependant_id == old_protocol.id:
                    dependant_id = new_protocol.id

                self.dependants_graph[protocol_id][index] = dependant_id

        if old_protocol.id in self.dependants_graph:
            self.dependants_graph[new_protocol.id] = self.dependants_graph.pop(old_protocol.id)

        self.final_value_reference.replace_protocol(old_protocol.id, new_protocol.id)
        self.final_uncertainty_reference.replace_protocol(old_protocol.id, new_protocol.id)


class DirectCalculationGraph:
    """A hierarchical structure for storing all protocols that need to be executed.
    """

    def __init__(self, root_directory=None):
        """Constructs a new DirectCalculationGraph

        Parameters
        ----------
        root_directory: str, optional
            The root directory in which to store all outputs from
            this graph.
        """
        self._nodes_by_id = {}

        self._root_nodes = []
        self._root_directory = root_directory

        self._dependants_graph = {}

        self._calculations_to_run = {}

    def _insert_node(self, protocol_name, calculation, parent_node_name=None):
        """Recursively inserts a protocol node into the tree.

        Parameters
        ----------
        protocol_name : str
            The name of the protocol to insert.
        calculation : DirectCalculation
            The calculation being inserted.
        parent_node_name : str, optional
            The name of the new parent of the node to be inserted. If None,
            the protocol will be added as a new parent node.
        """

        if protocol_name in self._nodes_by_id:

            raise RuntimeError('A protocol with id ' + protocol_name + ' has already been inserted'
                                                                       ' into the graph.')

        nodes = self._root_nodes if parent_node_name is None else self._dependants_graph[parent_node_name]

        protocol_to_insert = calculation.protocols[protocol_name]
        existing_node = None

        # Start by checking to see if the starting node of the calculation graph is
        # already present in the full graph.
        for node_id in nodes:

            node = self._nodes_by_id[node_id]

            if not node.can_merge(protocol_to_insert):
                continue

            existing_node = node
            break

        if existing_node is not None:
            # Make a note that the existing node should be used in place
            # of this calculations version.

            existing_node.merge(protocol_to_insert)
            calculation.replace_protocol(protocol_to_insert, existing_node)

        else:

            parent_node = None if parent_node_name is None else self._nodes_by_id[parent_node_name]
            root_directory = self._root_directory if parent_node_name is None else None

            protocol_to_insert.directory = protocol_to_insert.id if parent_node is None \
                else path.join(parent_node.directory, protocol_to_insert.id)

            if root_directory is not None:

                protocol_to_insert.directory = path.join(root_directory,
                                                         protocol_to_insert.directory)

            # Add the protocol as a new node in the graph.
            self._nodes_by_id[protocol_name] = protocol_to_insert

            existing_node = self._nodes_by_id[protocol_name]
            self._dependants_graph[protocol_name] = []

            if parent_node_name is None:
                self._root_nodes.append(protocol_name)
            else:

                for node_id in calculation.dependants_graph:

                    if (protocol_name not in calculation.dependants_graph[node_id] or
                       node_id in self._dependants_graph[protocol_name]):

                        continue

                    self._dependants_graph[node_id].append(protocol_name)

        reduced_graph = copy.deepcopy(calculation.dependants_graph)
        graph.apply_transitive_reduction(reduced_graph)

        # Add all of the dependants to the existing node
        for dependant_name in reduced_graph[existing_node.id]:
            self._insert_node(dependant_name, calculation, existing_node.id)

    def add_calculation(self, calculation):
        """Insert a calculation into the calculation graph.

        Parameters
        ----------
        calculation : DirectCalculation
            The calculation to insert.
        """

        if calculation.uuid in self._calculations_to_run:

            # Quick sanity check.
            raise ValueError('A calculation with the same uuid ({}) is '
                             'trying to run twice.'.format(calculation.uuid))

        self._calculations_to_run[calculation.uuid] = calculation

        for starting_protocol_name in calculation.starting_protocols:
            self._insert_node(starting_protocol_name, calculation)

        return

    def submit(self, backend):
        """Submits the protocol graph to the backend of choice.

        Parameters
        ----------
        backend: PropertyEstimatorBackend
            The backend to launch the graph on.

        Returns
        -------
        list of Future:
            The futures of the submitted protocols.
        """
        submitted_futures = {}
        value_futures = []

        # Determine the ideal order in which to submit the
        # protocols.
        submission_order = graph.topological_sort(self._dependants_graph)
        # Build a dependency graph from the dependants graph so that
        # futures can be passed in the correct place.
        dependencies = graph.dependants_to_dependencies(self._dependants_graph)

        for node_id in submission_order:

            node = self._nodes_by_id[node_id]
            dependency_futures = []

            for dependency in dependencies[node_id]:
                dependency_futures.append(submitted_futures[dependency])

            # Pull out any 'global' properties.
            global_properties = {}

            for input_reference in node.input_references:

                if input_reference.output_protocol_id != 'global':
                    continue

                global_properties[input_reference.output_property_name] = node.get_input_value(input_reference)

            submitted_futures[node_id] = backend.submit_task(DirectCalculationGraph._execute_protocol,
                                                             node.directory,
                                                             node.schema,
                                                             global_properties,
                                                             *dependency_futures)

        for calculation_id in self._calculations_to_run:

            calculation = self._calculations_to_run[calculation_id]

            value_node_id = calculation.final_value_reference.output_protocol_id
            uncertainty_node_id = calculation.final_uncertainty_reference.output_protocol_id

            # Gather the values and uncertainties of each property being calculated.
            value_futures.append(backend.submit_task(DirectCalculationGraph._gather_results,
                                                     submitted_futures[value_node_id],
                                                     calculation.final_value_reference,
                                                     submitted_futures[uncertainty_node_id],
                                                     calculation.final_uncertainty_reference,
                                                     calculation.physical_property))

        return value_futures

    @staticmethod
    def _execute_protocol(directory, protocol_schema, global_properties, *parent_outputs):
        """Executes a protocol defined by the input schema, and with
        inputs sets via the global scope and from previously executed protocols.


        """

        # Store the results of the relevant previous protocols in a handy dictionary.
        # If one of the results is a failure, propagate it up the chain!
        parent_outputs_by_id = {}

        for parent_id, parent_output in parent_outputs:

            parent_outputs_by_id[parent_id] = parent_output

            if isinstance(parent_output, protocols.PropertyCalculatorException):
                return protocol_schema.id, parent_output

        # Recreate the protocol on the backend to bypass the need for static methods
        # and awkward args and kwargs syntax.
        protocol = protocols.available_protocols[protocol_schema.type]()
        protocol.schema = protocol_schema

        # Try to set global properties on each of the protocols
        protocol.set_global_properties(global_properties)
        protocol.set_uuid(graph.retrieve_uuid(protocol.id))

        if not path.isdir(directory):
            os.makedirs(directory)

        for input_reference in protocol.input_references:

            if input_reference.output_protocol_id == 'global':
                continue

            input_value = parent_outputs_by_id[input_reference.output_protocol_id][
                                               input_reference.output_property_name]

            protocol.set_input_value(input_reference, input_value)

        try:
            output_dictionary = protocol.execute(directory)
        except Exception as e:
            # Except the unexpected...
            return protocol.id, protocols.PropertyCalculatorException(directory=directory,
                                                                      message='An unhandled exception '
                                                                              'occurred: {}'.format(e))

        return protocol.id, output_dictionary

    @staticmethod
    def _gather_results(value_result, value_reference, uncertainty_result,
                        uncertainty_reference, property_to_return):
        """Gather the value and uncertainty calculated from the submission graph
        and store them in the property to return.


        Todo
        ----
        * Docstrings

        Parameters
        ----------
        value_result: dict of string and Any
            ...
        value_property_name: str
            ...
        uncertainty_result: dict of string and Any
            ...
        uncertainty_property_name
            ...
        property_to_return: PhysicalProperty
            ...

        Returns
        -------
        (boolean, PhysicalProperty)
            ...
        """

        succeeded = True
        failure_object = None

        # Make sure none of the protocols failed and we actually have a value
        # and uncertainty.
        if isinstance(value_result[1], protocols.PropertyCalculatorException):
            # TODO: property_to_return.error_code = value_result

            failure_object = value_result[1]
            succeeded = False

        if isinstance(uncertainty_result[1], protocols.PropertyCalculatorException):
            # TODO: property_to_return.error_code = uncertainty_result

            failure_object = uncertainty_result[1]
            succeeded = False

        if succeeded:

            # TODO: Fill in provenance
            property_to_return.source = CalculationSource(fidelity=SimulationLayer.__name__,
                                                          provenance='')

            property_to_return.value = value_result[1][value_reference.output_property_name]
            property_to_return.uncertainty = uncertainty_result[1][uncertainty_reference.output_property_name]

        else:

            property_to_return.source = CalculationSource(fidelity=SimulationLayer.__name__,
                                                          provenance=failure_object.json())

        return True, property_to_return


# =============================================================================================
# Simulation Layer
# =============================================================================================

@register_calculation_layer()
class SimulationLayer(PropertyCalculationLayer):

    @staticmethod
    def _build_calculation_graph(properties, force_field_path, schemas):
        """ Construct a graph of the protocols needed to calculate a set of properties.

        Parameters
        ----------
        properties : list of PhysicalProperty
            The properties to attempt to compute.
        force_field_path : str
            The path to the force field parameters to use in the calculation.
        schemas : dict of str and CalculationSchema
            A list of the schemas to use when performing the calculations.
        """
        calculation_graph = DirectCalculationGraph('property-data')

        for property_to_calculate in properties:

            property_type = type(property_to_calculate).__name__

            if property_type not in schemas:

                logging.warning('The property calculator does not support {} '
                                'calculations.'.format(property_type))

                continue

            schema = schemas[property_type]

            calculation = DirectCalculation(property_to_calculate,
                                            force_field_path,
                                            schema)

            calculation_graph.add_calculation(calculation)

        return calculation_graph

    @staticmethod
    def schedule_calculation(backend, data_model, existing_data, callback, synchronous=False):

        calculation_graph = SimulationLayer._build_calculation_graph(data_model.queued_properties,
                                                                     data_model.parameter_set_path,
                                                                     data_model.options.calculation_schemas)

        simulation_futures = calculation_graph.submit(backend)

        PropertyCalculationLayer._await_results(backend, data_model, callback, simulation_futures, synchronous)