"""
Module used to help with driving the execution of analysis workflows
"""
import sys
import argparse

from omsi.dataformat.omsi_file.common import omsi_file_common
from omsi.dataformat.omsi_file.main_file import omsi_file
from omsi.workflow.driver.base import workflow_driver_base
from omsi.workflow.executor.base import workflow_executor_base
from omsi.workflow.common import RawDescriptionDefaultHelpArgParseFormatter
import omsi.shared.mpi_helper as mpi_helper
import numpy as np
import os
from omsi.shared.log import log_helper

# TODO We need to add saving of analyses to the workflow itself and allow saving to separate files
# TODO Need to add cabability to save workflow state after each analysis completes and ability to restart a workflow after it has been interrupted
# TODO We need to add command line options to control the workflow executor itself, e.g., to set the reduce_memory_usage option of the greedy_executor
# TODO The workflow executor needs to expose its parameters as well
# TODO We need a command-line option to define the workflow executor type
# TODO Add documentation on running workflows using the driver
# TODO Update docstrings of the cl_workflow_driver class (they are largely copy-paste from the analysis driver)


class cl_workflow_driver(workflow_driver_base):
    """
    Command-line workflow driver.

    :cvar output_save_arg_name: Name of the optional keyword argument for specifying the name
        and target for the analysis

    :cvar analysis_class_arg_name: Name of the optional first positional argument to be used
        to define the analysis class to be used.

    :cvar profile_arg_name: Name of the keyword argument used to enable profiling of the analysis

    :cvar profile_mem_arg_name: Name of the keyword argument used to enable profiling of memory usage of an analysis


    :ivar analysis_class: The class (subclass of analysis_base) defining the analysis to be executed
    :ivar add_script_arg: Boolean indicating whether an optional positional command line argument
        should be used to determine the analysis class (or whether the analysis class will be set explicitly)
    :ivar add_output_arg: Boolean indicating whether an optional keyword argument should be added to define
        the output target for the analysis.
    :ivar add_profile_arg: Add the optional --profile keyword argument for profiling the analysis
    :ivar profile_analyses: Boolean indicating whether we should profile the analysis
    :ivar parser: The argparse.ArgumentParser instance used for defining command-line arguments
    :ivar required_argument_group: argparse.ArgumentParser argument group used to define required command line arguments
    :ivar custom_argument_groups: Dict of custom argparse.ArgumentParser argument groups specified by the analysis
    :ivar output_target: Specification of the output target where the analysis result should be stored
    :ivar analysis_arguments: Dictionary defining the input arguments to be used for the analysis

    """
    script_arg_name = 'script'
    """The name where the positional argument for defining the analysis class will be stored."""

    output_save_arg_name = 'save'
    """Name of the key-word argument used to define"""

    profile_arg_name = 'profile'
    """Name of the keyword argument used to enable profiling of the analysis"""

    profile_mem_arg_name = 'memprofile'
    """Name of the keyword argument used to enable profiling of memory usage of an analysis"""

    log_level_arg_name = 'loglevel'
    """Name of the keyword argument used to specify the level of logging to be used"""

    def __init__(self,
                 workflow_executor,
                 add_script_arg=False,
                 add_output_arg=True,
                 add_log_level_arg=True,
                 add_profile_arg=False,
                 add_mem_profile_arg=False):
        """

        :param workflow_executor: The analysis class for which we want to execute the analysis.
            The analysis class must derive from omsi.analysis.analysis_base. May be None
            in case that we use the command-line to define the analysis class via the optional
            positional argument for the command class (i.e., set add_script_arg to True).
        :type workflow_executor: omsi.analysis.base.analysis_base
        :param add_script_arg: Boolean indicating whether we will use the positional
            command-line argument to determine the analysis class name
        :param add_output_arg: Boolean indicating whether we should add the optional keyword
            argument for defining the output target for the analysis.
        :param add_profile_arg: Boolean indicating whether we should add the optional keyword
            argument for enabling profiling of the analysis.
        :param add_mem_profile_arg: Boolean indicating whether we should add the optional
            keyword argument for enabling memory profiling of the analysis.

        :raises: A ValueError is raised in the case of conflicting inputs, i.e., if
            i) workflow_executor==None and add_script_arg=False, i.e., the analysis class is not determined or
            ii) workflow_executor!=None and add_script_arg=False, i.e, the analysis class is determined
            via two separate mechanisms.

        """
        # Check if the input is valid
        if workflow_executor is None and not add_script_arg:
            raise ValueError('The workflow executor must be either set explicitly or determined from the command line.')
        if workflow_executor is not None and add_script_arg:
            raise ValueError('Conflicting inputs: workflow_executor set and add_script_arg set to True.')
        super(cl_workflow_driver, self).__init__(workflow_executor)
        # self.workflow_executor = workflow_executor  # Initialized by the super constructor call
        self.workflow_executor = None
        self.script_files = []
        self.add_script_arg = add_script_arg
        self.add_output_arg = add_output_arg
        self.add_profile_arg = add_profile_arg
        self.add_mem_profile_arg = add_mem_profile_arg
        self.add_log_level_arg = add_log_level_arg
        self.parser = None   # The argument parser
        self.required_argument_group = None    # The argparse group used for required arguments
        self.optional_argument_group = None    # The argparse group used for general optional arguments
        # The string used to separate the analysis identifier from the argument name in command-line options
        self.identifier_argname_seperator = ":"
        self.output_target = None
        self.profile_analyses = False
        self.profile_analyses_mem = False
        self. __output_target_self = None  # Path to output target created by the driver if we need to remove it
        self.analysis_arguments = {}
        self.custom_argument_groups = {}
        self.mpi_root = 0
        self.mpi_comm = mpi_helper.get_comm_world()
        self.user_log_level = None   # The logging level specified by the user. A developer may have set some log_level in his script and we want to overwrite it

    def create_workflow_executor_object(self):
        """
        Initialize the workflow executor object, i.e., set self.workflow_executor

        *Side effects* This function potentially modifies self.workflow_executor

        """
        if self.workflow_executor is None:
            log_helper.debug(__name__, 'Initalizing workflow executor', root=self.mpi_root, comm=self.mpi_comm)
            default_executor_class = workflow_executor_base.get_default_executor_class()
            if self.script_files is None or len(self.script_files) == 0:
                self.workflow_executor = default_executor_class()
            else:
                self.workflow_executor = default_executor_class.from_script_files(self.script_files)
            self.workflow_executor.mpi_root = self.mpi_root
            self.workflow_executor.mpi_comm = self.mpi_comm
        else:
            pass

    def reset_workflow_executor_object(self):
        """
        Remove and recreate the workflow executor object
        """
        self.workflow_executor = None
        self.create_workflow_executor_object()

    def initialize_argument_parser(self):
        """
        Internal helper function used to initialize the argument parser.

        *Side effects:* The function sets:

            * ``self.parser``
            * ``self.required_argument_group``
            * ``self.opitonal_argument_group``

        """
        # Setup the argument parser
        parser_description = "Execute workflow based on a given set of scripts"
        parser_epilog = "how to specify ndarray data? \n" \
                        "---------------------------- \n" +\
                        "n-dimensional arrays stored in OpenMSI data files may be specified as \n" + \
                        "input parameters via the following syntax: \n" + \
                        "      -- MSI data: <filename>.h5:/entry_#/data_# \n" + \
                        "      -- Analysis data: <filename>.h5:/entry_#/analysis_#/<dataname> \n" + \
                        "      -- Arbitrary dataset: <filename>.h5:<object_path>\n" + \
                        "E.g. a valid definition may look like: 'test_brain_convert.h5:/entry_0/data_0'\n" + \
                        "In rear cases we may need to manually define an array (e.g., a mask)\n" + \
                        "Here we can use standard python syntax, e.g, '[1,2,3,4]' or '[[1, 3], [4, 5]]' \n" + \
                        "\n\n" + \
                        "This command-line tool has been auto-generated by BASTet (Berkeley Analysis & Storage Toolkit)"

        self.parser = argparse.ArgumentParser(description=parser_description,
                                              epilog=parser_epilog,
                                              formatter_class=RawDescriptionDefaultHelpArgParseFormatter,
                                              add_help=False)  # We'll add the help later in

        # Create the argument group for required and optional arguments
        self.required_argument_group = self.parser.add_argument_group(title="required arguments")
        self.optional_argument_group = self.parser.add_argument_group(title="optional arguments")

        # Add optional the script argument
        if self.add_script_arg:
            self.required_argument_group.add_argument("--"+self.script_arg_name,
                                                       action='append',
                                                       default=None,
                                                       type=str,
                                                       required=True,
                                                       help='The workflow script to be executed. Multiple scripts ' + \
                                                             'may be added via separate --script arguments')
        # Add the argument for defining where we should save the analysis
        if self.add_output_arg:
            output_arg_help = 'Define the file and experiment where all analysis results should be stored. ' + \
                              'A new file will be created if the given file does not exists ' + \
                              'but the directory does. The filename is expected to be of the from:  ' + \
                              '<filename>:<entry_#> . If no experiment index is given, then ' + \
                              'experiment index 0 (i.e, entry_0) will be assumed by default. A valid' + \
                              'path may, e.g, be "test.h5:/entry_0" or jus "test.h5"'
            self.optional_argument_group.add_argument("--"+self.output_save_arg_name,
                                     action='store',
                                     default=None,
                                     type=str,
                                     required=False,
                                     help=output_arg_help)

        # Add the optional keyword argument for enabling profiling of the analysis
        if self.add_profile_arg:
            profile_arg_help = 'Enable runtime profiling of the analysis. NOTE: This is intended for ' + \
                               'debugging and investigation of the runtime behavior of an analysis.' + \
                               'Enabling profiling entails certain overheads in performance'
            self.optional_argument_group.add_argument("--"+self.profile_arg_name,
                                     action='store_true',
                                     default=False,
                                     required=False,
                                     help=profile_arg_help)
        # Add the optional keyword argument for enabling memory profiling of the analysis
        if self.add_mem_profile_arg:
            profile_mem_arg_help = 'Enable runtime profiling of the memory usage of analysis. ' + \
                                   'NOTE: This is intended for debugging and investigation of ' + \
                                   'the runtime behavior of an analysis. Enabling profiling ' + \
                                   'entails certain overheads in performance.'
            self.optional_argument_group.add_argument("--"+self.profile_mem_arg_name,
                                     action='store_true',
                                     default=False,
                                     required=False,
                                     help=profile_mem_arg_help)
        # Add the optional logging argument
        if self.add_log_level_arg:
            self.optional_argument_group.add_argument("--"+self.log_level_arg_name,
                                     action='store',
                                     default='INFO',
                                     required=False,
                                     help='Specify the level of logging to be used.',
                                     choices=log_helper.log_levels.keys())

    def parse_cl_arguments(self):
        """
        The function assumes that the command line parser has been setup using the initialize_argument_parser(..)

        This function parses all arguments that are specific to the command-line parser itself. Analysis
        arguments are added and parsed later by the add_and_parse_analysis_arguments(...) function.
        The reason for this is two-fold: i) to separate the parsing of analysis arguments and arguments of the
        command-line driver and ii) if the same HDF5 file is used as input and output target, then we need to
        open it first here in append mode before it gets opened in read mode later by the arguments.

        *Side effects:* The function sets:

            - ``self.output_target``
            - ``self.profile_analyses``

        """
        # Parse the arguments and convert them to a dict using vars
        parsed_arguments = vars(self.parser.parse_known_args()[0])

        # Process the --save argument to determine where we should save the output
        if self.output_save_arg_name in parsed_arguments and mpi_helper.get_rank() == self.mpi_root:
            # Determine the filename and experiment group from the path
            self.output_target = parsed_arguments.pop(self.output_save_arg_name)
            if self.output_target is not None:
                output_filename, output_object_path = omsi_file_common.parse_path_string(self.output_target)
                # Create the output file
                if output_filename is None:
                    raise ValueError("ERROR: Invalid save parameter specification " + self.output_target)
                elif os.path.exists(output_filename) and not os.path.isfile(output_filename):
                    raise ValueError("ERROR: Save parameter not specify a file.")
                if not os.path.exists(output_filename):
                    out_file = omsi_file(output_filename, mode='a')
                    self.output_target = out_file.create_experiment()
                    self. __output_target_self = output_filename
                else:
                    out_file = omsi_file(output_filename, mode='r+')
                    if output_object_path is not None:
                        self.output_target = omsi_file_common.get_omsi_object(out_file[output_object_path])
                    else:
                        if out_file.get_num_experiments() > 0:
                            self.output_target = out_file.get_experiment(0)
                        else:
                            self.output_target = out_file.create_experiment()
        else:
            self.output_target = parsed_arguments.pop(self.output_save_arg_name)

        # Process the --profile profiling argument
        if self.profile_arg_name in parsed_arguments:
            self.profile_analyses = parsed_arguments.pop(self.profile_arg_name)

        # Process the --memprofile argument
        if self.profile_mem_arg_name in parsed_arguments:
            self.profile_analyses_mem = parsed_arguments.pop(self.profile_mem_arg_name)

        # The --loglevel argument
        if self.log_level_arg_name in parsed_arguments:
            self.user_log_level = parsed_arguments.pop(self.log_level_arg_name)
            if self.user_log_level in log_helper.log_levels.keys():
                log_helper.set_log_level(level=log_helper.log_levels[self.user_log_level])
            else:
                self.user_log_level = None
                log_helper.error(module_name=__name__, message="Invalid log level specified")

        # The --script arguments
        if self.script_arg_name in parsed_arguments:
            self.script_files = parsed_arguments.pop(self.script_arg_name)
            if self.workflow_executor is None:
                self.create_workflow_executor_object()
            else:
                self.workflow_executor.add_analysis_from_scripts(script_files=self.script_files)


    def add_and_parse_analysis_arguments(self):
        """
        The function assumes that the command line parser has been setup using the initialize_argument_parser(..)

        This function is responsible for adding all command line arguments that are specific to the workflow and
        to then parse those arguments and save the relevant data in the self.analysis_arguments dictionary.
        Command-line arguments that are specific to the command line driver are removed, so that only
        arguments that can be consumed by the analysis are handed to the analysis.

        *Side effects:* The function sets ``self.analysis_arguments`` and updates the analysis parameters of the
                        analyses stored in ``self.workflow_executor.analysis_tasks``

        """
        # Ensure that we have a workflow executor instantiated. This should usually not happen.
        if self.workflow_executor is None:
            log_helper.warning(__name__, "Late creation of the workflow executor in add_and_parse_analysis_arguments",
                               root=self.mpi_root, comm=self.mpi_comm)
            self.create_workflow_executor_object()

        # Ensure that all analysis identifiers are set to a defined value
        self.workflow_executor.analysis_tasks.set_undefined_analysis_identifiers()

        # Ensure that all analysis identifiers are unique
        if not self.workflow_executor.analysis_tasks.analysis_identifiers_unique():
            log_helper.warning("The workflow contains multiple analyses with the same user-defined identifier. " +
                               "Colliding identifiers will be modified to ensure uniqueness")
            self.workflow_executor.analysis_tasks.make_analysis_identifiers_unique(self)

        # Add all arguments from our workflow executor
        target_seperator = self.identifier_argname_seperator
        if self.workflow_executor is not None:
            for analysis in self.workflow_executor:
                # Create the group for the analysis in general
                analysis_group = self.parser.add_argument_group(title=analysis.get_analysis_identifier() + " : " +
                                                                      analysis.get_analysis_type())
                arg_group_name = analysis.get_analysis_identifier() + target_seperator + analysis.get_analysis_type()
                self.custom_argument_groups[arg_group_name] = analysis_group

                # Create groups for all argument groups of the analysis
                analysis_arg_groups = {}
                arg_group_dict = {arg_param.get_group_name(): arg_param.get_group_description()
                                  for arg_param in analysis.get_all_parameter_data()
                                  if arg_param.get_group_name() is not None}
                for group_name, group_description in arg_group_dict.iteritems():
                    ana_arg_group_name = arg_group_name + ":" + group_name
                    analysis_arg_groups[group_name] = self.parser.add_argument_group(title=ana_arg_group_name,
                                                                                     description=group_description)
                    self.custom_argument_groups[ana_arg_group_name] = analysis_arg_groups[group_name]

                # Add all undefined parameters of the current analysis
                for arg_param in analysis.get_all_parameter_data():
                    # If the parameter is notset
                    if not arg_param.data_set():
                        # Add the parameter to the argument parser
                        arg_name = "--" + analysis.get_analysis_identifier() + \
                                   self.identifier_argname_seperator + arg_param['name']
                        arg_action = 'store'
                        arg_default = arg_param['default']
                        arg_required = arg_param['required'] and (arg_default is None)
                        arg_type = arg_param['dtype']
                        arg_choices = arg_param['choices']
                        arg_help = arg_param['help']
                        arg_dest = analysis.get_analysis_identifier() + target_seperator + arg_param['name']
                        arg_group = arg_param.get_group_name()

                        # Determine the group the argument belongs to
                        argument_group = self.required_argument_group if arg_required else analysis_group
                        if arg_group in analysis_arg_groups:
                            argument_group = analysis_arg_groups[arg_group]

                        # Add the argument to the proper group
                        argument_group.add_argument(arg_name,               # <-- Required, user specified arg name
                                                    action=arg_action,      #     Constant. We define this not the user.
                                                    # nargs=1,                    Don't use. Leave as default
                                                    # const=None,                 Don't use. We don't use this type of action
                                                    default=arg_default,    # <-- Optional default value for the argument
                                                    type=arg_type,          # <-- Optional dtype of the argument
                                                    choices=arg_choices,    # <-- Optional Key may be missing.
                                                    required=arg_required,  # <-- Optional
                                                    help=arg_help,          # <-- Required
                                                    # metavar               #     Don't use. Positional analysis arguments
                                                    #                       #     are not allowed
                                                    dest=arg_dest)          #     Automatically determined by the name
        # Add the help argument
        self.optional_argument_group.add_argument('-h', '--help',
                                 action='help',
                                 default=argparse.SUPPRESS,
                                 help='show this help message and exit')
        parsed_arguments = vars(self.parser.parse_args())
        parsed_arguments.pop(self.profile_arg_name, None)
        parsed_arguments.pop(self.output_save_arg_name, None)
        parsed_arguments.pop(self.profile_mem_arg_name, None)
        parsed_arguments.pop(self.log_level_arg_name, None)
        parsed_arguments.pop(self.script_arg_name, None)

        self.analysis_arguments = parsed_arguments

        # Update the workflow to set all analysis arguments
        for arg_key, arg_value in self.analysis_arguments.iteritems():
            ana_identifier, arg_key = arg_key.split(target_seperator)
            self.workflow_executor.analysis_tasks[ana_identifier][arg_key] = arg_value
            # Make sure we use the user-specified log level, even if it is set differently in the scripts
            if self.user_log_level is not None:
                log_helper.set_log_level(level=log_helper.log_levels[self.user_log_level])

    def print_settings(self):
        """
        Print the analysis settings.
        """
        log_helper.info(__name__, "Inputs:")
        for key, value in self.analysis_arguments.iteritems():
            log_helper.info(__name__, "   " + unicode(key) + " = " + unicode(value))
        if self.output_target is not None:
            if isinstance(self.output_target, omsi_file_common):
                h5py_object = omsi_file_common.get_h5py_object(self.output_target)
                log_helper.info(__name__, "Save to: " + unicode(h5py_object.file.filename)
                                + u":" + unicode(h5py_object.name))
            else:
                log_helper.info(__name__, "Save to: " + unicode(self.output_target))

    def print_time_and_usage_profiles(self):
        """
        Print the profiling data for time and usage if available
        """
        for analysis in self.workflow_executor.analysis_tasks:
            stats_obj = analysis.get_profile_stats_object(consolidate=True)
            if stats_obj is not None:
                print ""
                print "PROFILING DATA: TIME AND USAGE: " + analysis.get_analysis_identifier() + " : " + \
                    analysis.get_analysis_type()
                print ""
                stats_obj.print_stats()

    def print_memory_profiles(self):
        """
        Print the memory profiles if available
        """
        # Print the profiling results for memory usage
        for analysis in self.workflow_executor.analysis_tasks:
            mem_profile_info = analysis.get_memory_profile_info()
            if mem_profile_info is not None:
                print ""
                print "PROFILING DATA: MEMORY"
                print ""
                print mem_profile_info

    def remove_output_target(self):
        """
        This function is used to delete any output target files created by the
        command line driver. This is done in case that an error occurred and
        we do not want to leave garbage files left over.

        *Side effects* The function modifies ``self.output_target``

        :return: Boolean indicating whether we succesfully cleaned up the output
        """
        success = False
        if self.__output_target_self is not None:
            try:
                os.remove(self.__output_target_self)
                log_helper.info(__name__, "Successfully removed output target: " + unicode(self.__output_target_self))
                success = True
            except:
                log_helper.error(__name__, "Clean-up of output failed. File may be left on system: "
                                 + unicode(self.__output_target_self))
        elif self.output_target is not None:
            log_helper.info(__name__, "Output target not removed because it was not created " +
                                      "by the analysis but potentially modified by it")
        else:
            success = True
        return success

    def main(self):
        """
        Default main function for running an analysis from the command line.
        The default implementation exposes all specified analysis parameters as command
        line options to the user. The default implementation also provides means to
        print a help text for the function.

        :raises: ValueError is raised in case that the analysis class is unknown
        """

        # Initialize the argument parser
        if self.parser is None:
            self.initialize_argument_parser()

        try:
            # Parse the command line arguments to determine the command line driver settings
            self.parse_cl_arguments()
        except:
            self.remove_output_target()
            raise

        if self.workflow_executor is None:
            self.remove_output_target()
            log_helper.error(__name__, 'Missing --script parameter or worfklow_executor object')
            raise ValueError('Workflow not initalized')

        # Add and parse the command line arguments specific to the analysis to determine the analysis settings
        try:
            self.add_and_parse_analysis_arguments()
        except:
            self.remove_output_target()
            raise

        # Print the analysis settings
        if mpi_helper.get_rank() == self.mpi_root:
            self.print_settings()

        # Enable time and usage profiling
        try:
            # Enable time and usage profiling if requested
            if self.profile_analyses:
                try:
                    self.workflow_executor.analysis_tasks.enable_time_and_usage_profiling(self.profile_analyses)
                except ImportError as e:
                    log_helper.warning(__name__, "Profiling of time and usage not available due to missing packages.")
                    log_helper.warning(__name__, e.message)
            # Enable memory profiling if requested
            if self.profile_analyses_mem:
                try:
                    self.workflow_executor.analysis_tasks.enable_memory_profiling(self.profile_analyses_mem)
                except ImportError as e:
                    log_helper.warning(__name__, "Profiling of memory usage not available due to missing packages")
                    log_helper.warning(__name__, e.message)
        except:
            if mpi_helper.get_rank() == self.mpi_root:
                self.remove_output_target()
            raise

        # Execute the analysis
        try:
            log_helper.debug(__name__, 'Analysis arguments: ' + str(self.analysis_arguments),
                             root=self.mpi_root, comm=self.mpi_comm)
            self.workflow_executor.execute()
        except:
            if mpi_helper.get_rank() == self.mpi_root:
                self.remove_output_target()
            raise

        # Finalize the saving of results on rank our mpi root rank. NOTE: When running in serial
        # the condition of  mpi_helper.get_rank() ==  self.mpi_root evaluates to True because
        # our mpi_root is 0 and the mpi_helper returns 0 for the rank when running in serial.
        if mpi_helper.get_rank() == self.mpi_root:

            # Print usage profiles if available
            try:
                self.print_time_and_usage_profiles()
            except:
                log_helper.error(__name__, "An error occured while trying to print time and usage profiles",
                                 root=self.mpi_root, comm=self.mpi_comm)

            # Print memory profile data if available
            try:
                self.print_memory_profiles()
            except:
                log_helper.error(__name__, "An error occured while trying to print memory profiles",
                                 root=self.mpi_root, comm=self.mpi_comm)

            # Print the time it took to run the analysis
            try:
                # Parallel case: We need to compile/collect timing data from all cores
                if isinstance(self.workflow_executor.run_info['execution_time'], list):
                    # Time for each task to execute
                    log_helper.info(__name__, "Time in seconds for each analysis process: " +
                                    str(self.workflow_executor.run_info['execution_time']),
                                    root=self.mpi_root, comm=self.mpi_comm)
                    # Start times of each task
                    log_helper.info(__name__, "Time when each of the processes started: " +
                                    str(self.workflow_executor.run_info['start_time']),
                                    root=self.mpi_root, comm=self.mpi_comm)
                    # Stop times for each task

                    log_helper.info(__name__, "Time when each of the processes finished: " +
                                    str(self.workflow_executor.run_info['end_time']),
                                    root=self.mpi_root, comm=self.mpi_comm)

                    # Compile the time to execute string
                    exec_time_array = np.asarray(self.workflow_executor.run_info['execution_time'], dtype=float)
                    max_exec_time = str(exec_time_array.max())
                    min_exec_time = str(exec_time_array.min())
                    mean_exec_time = str(exec_time_array.mean())
                    exec_time_string = max_exec_time + " s " + \
                                       "    ( min = " + min_exec_time + " , mean = " + mean_exec_time + " )"
                # Serial case: We only have a single time to worry about
                else:
                    exec_time_string = str(self.workflow_executor.run_info['execution_time']) + " s"
                log_helper.info(__name__, "Time to execute analysis: " + exec_time_string,
                                root=self.mpi_root, comm=self.mpi_comm)
            except:
                raise

        # Save the analysis to file
        if self.output_target is not None:
            from omsi.dataformat.omsi_file.analysis import omsi_analysis_manager
            for analysis in self.workflow_executor.analysis_tasks:
                omsi_analysis_manager.create_analysis_static(analysis_parent=self.output_target,
                                                             analysis=self.analysis)

            # TODO we should compute the minimum and maximum start time and compute the total runtime that way as well
            # TODO add MPI Barrier at the beginning to make sure everyone has started up before we do anything

        # print self.workflow_executor[2]['output_0'][:,:,24]

if __name__ == "__main__":

    # Create an command-line driver and call the main function to run the analysis
    cl_workflow_driver(workflow_executor=None,
                       add_script_arg=True,
                       add_output_arg=True,
                       add_profile_arg=True,
                       add_mem_profile_arg=True).main()
