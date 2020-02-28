# -*- coding: utf-8 -*-
"""This class maintains the internal dfTimewolf state.

Use it to track errors, abort on global failures, clean up after modules, etc.
"""

from __future__ import print_function
from __future__ import unicode_literals

import sys
import threading
import traceback

from dftimewolf.lib import errors
from dftimewolf.lib import utils
from dftimewolf.lib.modules import manager as modules_manager


class DFTimewolfState(object):
  """The main State class.

  Attributes:
    command_line_options (dict[str, str]): Command line options passed to
        dftimewolf.
    config (dftimewolf.config.Config): Class to be used throughout execution.
    errors (list[tuple[str, bool]]): errors generated by a module. These
        should be cleaned up after each module run using the CleanUp() method.
    global_errors (list[tuple[str, bool]]): the CleanUp() method moves non
        critical errors to this attribute for later reporting.
    input (list[str]): data that the current module will use as input.
    output (list[str]): data that the current module generates.
    recipe: (dict[str, str]): recipe declaring modules to load.
    store (dict[str, object]): arbitrary data for modules.
  """

  def __init__(self, config):
    """Initializes a state."""
    super(DFTimewolfState, self).__init__()
    self.command_line_options = {}
    self._module_pool = {}
    self._store_lock = threading.Lock()
    self._threading_event_per_module = {}
    self.config = config
    self.errors = []
    self.global_errors = []
    self.input = []
    self.output = []
    self.recipe = None
    self.store = {}
    self.streaming_callbacks = {}

  def _InvokeModulesInThreads(self, callback):
    """Invokes the callback function on all the modules in separate threads.

    Args:
      callback (function): callback function to invoke on all the modules.
    """
    threads = []
    for module_definition in self.recipe['modules']:
      thread_args = (module_definition, )
      thread = threading.Thread(target=callback, args=thread_args)
      threads.append(thread)
      thread.start()

    for thread in threads:
      thread.join()

    self.CheckErrors(is_global=True)

  def LoadRecipe(self, recipe):
    """Populates the internal module pool with modules declared in a recipe.

    Args:
      recipe (dict[str, str]): recipe declaring modules to load.

    Raises:
      RecipeParseError: if a module in the recipe does not exist.
    """
    self.recipe = recipe
    module_definitions = recipe.get('modules', [])
    preflight_definitions = recipe.get('preflights', [])
    for module_definition in module_definitions + preflight_definitions:
      # Combine CLI args with args from the recipe description
      module_name = module_definition['name']
      module_class = modules_manager.ModulesManager.GetModuleByName(module_name)
      if not module_class:
        raise errors.RecipeParseError(
            'Recipe uses unknown module: {0:s}'.format(module_name))

      self._module_pool[module_name] = module_class(self)

  def StoreContainer(self, container):
    """Thread-safe method to store data in the state's store.

    Args:
      container (AttributeContainer): data to store.
    """
    with self._store_lock:
      self.store.setdefault(container.CONTAINER_TYPE, []).append(container)

  def GetContainers(self, container_class):
    """Thread-safe method to retrieve data from the state's store.

    Args:
      container_class (type): AttributeContainer class used to filter data.

    Returns:
      list[AttributeContainer]: attribute container objects provided in
          the store that correspond to the container type.
    """
    with self._store_lock:
      return self.store.get(container_class.CONTAINER_TYPE, [])

  def _SetupModuleThread(self, module_definition):
    """Calls the module's SetUp() function and sets a threading event for it.

    Callback for _InvokeModulesInThreads.

    Args:
      module_definition (dict[str, str]): recipe module definition.
    """
    module_name = module_definition['name']

    new_args = utils.ImportArgsFromDict(
        module_definition['args'], self.command_line_options, self.config)
    module = self._module_pool[module_name]

    try:
      module.SetUp(**new_args)
    except Exception as exception:  # pylint: disable=broad-except
      self.AddError(
          'An unknown error occurred: {0!s}\nFull traceback:\n{1:s}'.format(
              exception, traceback.format_exc()),
          critical=True)

    self._threading_event_per_module[module_name] = threading.Event()
    self.CleanUp()

  def SetupModules(self):
    """Performs setup tasks for each module in the module pool.

    Threads declared modules' SetUp() functions. Takes CLI arguments into
    account when replacing recipe parameters for each module.
    """
    # Note that vars() copies the values of argparse.Namespace to a dict.
    self._InvokeModulesInThreads(self._SetupModuleThread)

  def _RunModuleThread(self, module_definition):
    """Runs the module's Process() function.

    Callback for _InvokeModulesInThreads.

    Waits for any blockers to have finished before running Process(), then
    sets an Event flag declaring the module has completed.

    Args:
      module_definition (str): module definition.
    """
    module_name = module_definition['name']

    for dependency in module_definition['wants']:
      self._threading_event_per_module[dependency].wait()

    module = self._module_pool[module_name]

    try:
      module.Process()
    except errors.DFTimewolfError as exception:
      self.AddError(exception.message, critical=True)
    except Exception as exception:  # pylint: disable=broad-except
      self.AddError(
          'An unknown error occurred: {0!s}\nFull traceback:\n{1:s}'.format(
              exception, traceback.format_exc()),
          critical=True)

    print('Module {0:s} completed'.format(module_name))
    self._threading_event_per_module[module_name].set()
    self.CleanUp()

  def RunPreflights(self):
    """Runs preflight modules."""
    for preflight_definition in self.recipe.get('preflights', []):
      preflight_name = preflight_definition['name']
      args = preflight_definition.get('args', {})

      new_args = utils.ImportArgsFromDict(
          args, self.command_line_options, self.config)
      preflight = self._module_pool[preflight_name]
      try:
        preflight.SetUp(**new_args)
        preflight.Process()
      finally:
        self.CheckErrors(is_global=True)

  def InstantiateModule(self, module_name):
    """Instantiates an arbitrary dfTimewolf module.

    Args:
      module_name (str): The name of the module to instantiate.

    Returns:
      BaseModule: An instance of a dftimewolf Module, which is a subclass of
          BaseModule.
    """
    module_class = modules_manager.ModulesManager.GetModuleByName(module_name)
    return module_class(self)

  def RunModules(self):
    """Performs the actual processing for each module in the module pool."""
    self._InvokeModulesInThreads(self._RunModuleThread)

  def RegisterStreamingCallback(self, target, container_type):
    """Registers a callback for a type of container.

    The function to be registered should a single parameter of type
    interface.AttributeContainer.

    Args:
      target (function): function to be called.
      container_type (type[interface.AttributeContainer]): container type on
          which the callback will be called.
    """
    if container_type not in self.streaming_callbacks:
      self.streaming_callbacks[container_type] = []
    self.streaming_callbacks[container_type].append(target)

  def StreamContainer(self, container):
    """Streams a container to the callbacks that are registered to handle it.

    Args:
      container (interface.AttributeContainer): container instance that will be
          streamed to any registered callbacks.
    """
    for callback in self.streaming_callbacks.get(type(container), []):
      callback(container)

  def AddError(self, error, critical=False):
    """Adds an error to the state.

    Args:
      error (str): text that will be added to the error list.
      critical (Optional[bool]): True if dfTimewolf cannot recover from
          the error and should abort.
    """
    self.errors.append((error, critical))

  def CleanUp(self):
    """Cleans up after running a module.

    The state's output becomes the input for the next stage. Any errors are
    moved to the global_errors attribute so that they can be reported at a
    later stage.
    """
    # Move any existing errors to global errors
    self.global_errors.extend(self.errors)
    self.errors = []

    # Make the previous module's output available to the next module
    self.input = self.output
    self.output = []

  def CheckErrors(self, is_global=False):
    """Checks for errors and exits if any of them are critical.

    Args:
      is_global (Optional[bool]): True if the global_errors attribute should
          be checked. False if the error attribute should be checked.
    """
    error_objects = self.global_errors if is_global else self.errors
    if error_objects:
      print('dfTimewolf encountered one or more errors:')
      for error, critical in error_objects:
        print('{0:s}  {1!s}'.format('CRITICAL: ' if critical else '', error))
        if critical:
          print('Critical error found. Aborting.')
          sys.exit(1)
