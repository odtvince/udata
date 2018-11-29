'''
Data migrations logic
'''
import importlib.util
import inspect
import logging
import os
import queue

from datetime import datetime
from logging.handlers import QueueHandler
from flask import current_app
from mongoengine.connection import get_db
from pymongo import ReturnDocument
from pkg_resources import resource_isdir, resource_listdir, resource_filename, resource_string

from udata import entrypoints

log = logging.getLogger(__name__)


class MigrationError(Exception):
    '''
    Raised on migration execution error.

    :param msg str: A human readable message (a reason)
    :param output str: An optionnal array of logging output
    :param exc Exception: An optionnal underlying exception
    '''
    def __init__(self, msg, output=None, exc=None):
        super().__init__(msg)
        self.msg = msg
        self.output = output
        self.exc = exc


class RollbackError(MigrationError):
    '''
    Raised on rollback.
    Hold the initial migration error and rollback exception (if any)
    '''
    def __init__(self, msg, output=None, exc=None, migrate_exc=None):
        super().__init__(msg)
        self.msg = msg
        self.output = output
        self.exc = exc
        self.migrate_exc = migrate_exc


class MigrationFormatter(logging.Formatter):
    pass


class Record(dict):
    '''
    A simple wrapper to migrations document
    '''
    __getattr__ = dict.get

    def exists(self):
        return bool(self._id)

    @property
    def collection(self):
        return get_db().migrations

    @property
    def status(self):
        '''
        Status is the status of the last operation.

        Will be `None` is the record doesn't exists.
        Possible values are:
          - success
          - rollback
          - rollback-error
          - recorded
        '''
        if not self.exists():
            return
        op = self.ops[-1]
        if op['success']:
            if op['type'] == 'migrate':
                return 'success'
            elif op['type'] == 'rollback':
                return 'rollback'
            elif op['type'] == 'record':
                return 'recorded'
            else:
                return 'unknown'
        else:
            return 'rollback-error' if op['type'] == 'rollback' else 'error'

    @property
    def ok(self):
        '''
        Is true if the migration is considered as successfully applied
        '''
        if not self.exists():
            return False
        op = self.ops[-1]
        return op['success'] and op['type'] in ('migrate', 'record')

    def add(self, type, migration, output, state, success):
        script = inspect.getsource(migration)
        return Record(self.collection.find_one_and_update(
            {'plugin': self.plugin, 'filename': self.filename},
            {
                '$push': {'ops': {
                    'date': datetime.now(),
                    'type': type,
                    'script': script,
                    'output': output,
                    'state': state,
                    'success': success,
                }}
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        ))

    def delete(self):
        return self.collection.delete_one({'_id': self._id})


def get_record(plugin, filename):
    '''Get an existing migration record if exists'''
    data = get_db().migrations.find_one({'plugin': plugin, 'filename': filename})
    return Record(data or {'plugin': plugin, 'filename': filename})


def record(plugin, filename, module_name=None, dryrun=False):
    '''Only record a migration without applying it'''
    record = get_record(plugin, filename)
    if record.ok:
        return False
    try:
        path = _migration_path(plugin, filename, module_name)
    except Exception:
        return False
    if not os.path.exists(path):
        return False
    migration = _load_migration(plugin, filename, module_name=module_name)
    record.add('migrate', migration, 'Recorded only', {}, True)
    return True


def unrecord(plugin, filename):
    '''Only record a migration without applying it'''
    record = get_record(plugin, filename)
    if not record.exists():
        return False
    record.delete()
    return True


def list_availables():
    '''
    List available migrations for udata and enabled plugins

    Each row is tuple with following signature:

        (plugin, package, filename)
    '''
    migrations = []

    migrations.extend(_iter('udata', 'udata'))

    plugins = entrypoints.get_enabled('udata.models', current_app)
    for plugin, module in plugins.items():
        migrations.extend(_iter(plugin, module))
    return sorted(migrations, key=lambda r: r[2])


def execute(plugin, filename, module_name=None, recordonly=False, dryrun=False):
    '''
    Execute a migration given a plugin (and optionnaly its resolved module name) and a filename

    If recordonly is True, the migration is only recorded
    If dryrun is True, the migration is neither executed nor recorded
    '''
    migration = _load_migration(plugin, filename, module_name)
    record = get_record(plugin, filename)

    q = queue.Queue(-1)  # no limit on size
    handler = QueueHandler(q)
    handler.setFormatter(MigrationFormatter())
    logger = getattr(migration, 'log', logging.getLogger(migration.__name__))
    logger.propagate = False
    for h in logger.handlers:
        logger.removeHandler(h)
    logger.addHandler(handler)

    if not hasattr(migration, 'migrate'):
        error = SyntaxError('A migration should at least have a migrate(db) function')
        raise MigrationError('Error while executing migration', exc=error)

    if not recordonly and not dryrun:
        db = get_db()
        db._state = {}
        try:
            migration.migrate(db)
        except Exception as e:
            out = _extract_output(q)
            record.add('migrate', migration, out, db._state, False)
            fe = MigrationError('Error while executing migration',
                                output=out, exc=e)
            if hasattr(migration, 'rollback'):
                try:
                    migration.rollback(db)
                    out = _extract_output(q)
                    record.add('rollback', migration, out, db._state, True)
                    fe = RollbackError('Error while executing migration, rollback has been applyied',
                                       output=out,
                                       migrate_exc=fe)
                except Exception as re:
                    out = _extract_output(q)
                    record.add('rollback', migration, out, db._state, False)
                    fe = RollbackError('Error while executing migration rollback',
                                       output=out, exc=re, migrate_exc=fe)
            raise fe
        
    out = _extract_output(q)
    if not dryrun:
        record.add('migrate', migration, out, db._state, True)

    return out


def _iter(plugin, module):
    '''
    Iter over migrations for a given plugin module

    Yield tuples in the form (plugin_name, module_name, filename)
    '''
    module_name = module if isinstance(module, str) else module.__name__
    if not resource_isdir(module_name, 'migrations'):
        return
    for filename in resource_listdir(module_name, 'migrations'):
        if filename.endswith('.py') and not filename.startswith('__'):
            yield (plugin, module_name, filename)


def _module_name(plugin):
    '''Get the module namefor a given plugin'''
    if plugin == 'udata':
        return 'udata'
    module = entrypoints.get_plugin_module('udata.models', current_app, plugin)
    if module is None:
        raise ValueError('Plugin {} not found'.format(plugin))
    return module.__name__


def _migration_path(plugin, filename, module_name=None):
    '''Get a migration file path'''
    module_name = module_name or _module_name(plugin)
    filename = os.path.join('migrations', filename)
    return resource_filename(module_name, filename)


def _load_migration(plugin, filename, module_name=None):
    '''
    Load a migration from its python file

    :returns: the loaded module
    '''
    module_name = module_name or _module_name(plugin)
    basename = os.path.splitext(os.path.basename(filename))[0]
    name = '.'.join((module_name, 'migrations', basename))
    filename = os.path.join('migrations', filename)
    script = resource_string(module_name, filename)
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(script, module.__dict__)
    module.__file__ = resource_filename(module_name, filename)
    return module


def _extract_output(q):
    '''Extract log output from a QueueHandler queue'''
    out = []
    while not q.empty():
        record = q.get()
        out.append((record.levelname.lower(), record.getMessage()))
    return out
