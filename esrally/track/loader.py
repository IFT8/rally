import json
import logging
import os
import sys
import urllib.error
import importlib.machinery
import types

import jinja2
import jinja2.exceptions
import jsonschema
import tabulate
from esrally import exceptions, time, PROGRAM_NAME
from esrally.track import params, track
from esrally.utils import io, convert, net, git, versions, console

logger = logging.getLogger("rally.track")


class TrackSyntaxError(exceptions.InvalidSyntax):
    """
    Raised whenever a syntax problem is encountered when loading the track specification.
    """
    pass


def tracks(cfg):
    """

    Lists all known tracks. Note that users can specify a distribution version so if different tracks are available for
    different versions, this will be reflected in the output.

    :param cfg: The config object.
    :return: A list of tracks that are available for the provided distribution version or else for the master version.
    """
    repo = TrackRepository(cfg)
    reader = TrackFileReader(cfg)
    distribution_version = cfg.opts("source", "distribution.version", mandatory=False)
    data_root = cfg.opts("benchmarks", "local.dataset.cache")
    return [reader.read(track_name,
                        repo.track_file(distribution_version, track_name),
                        repo.track_dir(track_name),
                        "%s/%s" % (data_root, track_name.lower())
                        )
            for track_name in repo.track_names(distribution_version)]


def list_tracks(cfg):
    console.println("Available tracks:\n")
    console.println(tabulate.tabulate(
        tabular_data=[[t.name, t.short_description, ",".join(map(str, t.challenges))] for t in tracks(cfg)],
        headers=["Name", "Description", "Challenges"]))


def load_track(cfg):
    """

    Loads a track

    :param cfg: The config object. It contains the name of the track to load.
    :return: The loaded track.
    """
    track_name = cfg.opts("benchmarks", "track")
    try:
        repo = TrackRepository(cfg)
        reader = TrackFileReader(cfg)
        distribution_version = cfg.opts("source", "distribution.version", mandatory=False)
        data_root = cfg.opts("benchmarks", "local.dataset.cache")
        return reader.read(track_name, repo.track_file(distribution_version, track_name), repo.track_dir(track_name),
                           "%s/%s" % (data_root, track_name.lower()))
    except FileNotFoundError:
        logger.exception("Cannot load track [%s]" % track_name)
        raise exceptions.SystemSetupError("Cannot load track %s. List the available tracks with %s list tracks." %
                                          (track_name, PROGRAM_NAME))


def load_track_plugins(cfg, register_runner):
    track_name = cfg.opts("benchmarks", "track")
    distribution_version = cfg.opts("source", "distribution.version", mandatory=False)

    repo = TrackRepository(cfg, fetch=False)
    plugin_reader = TrackPluginReader(register_runner)

    track_plugin_file = repo.plugin_file(distribution_version, track_name)
    if os.path.exists(track_plugin_file):
        logger.info("Reading track plugin file [%s]." % track_plugin_file)
        plugin_reader(track_plugin_file)
    else:
        logger.info("Skipping plugin detection for this track ([%s] does not exist)." % track_plugin_file)


def operation_parameters(t, op):
    if op.param_source:
        logger.debug("Creating parameter source with name [%s]" % op.param_source)
        return params.param_source_for_name(op.param_source, t.indices, op.params)
    else:
        logger.debug("Creating parameter source for operation type [%s]" % op.type)
        return params.param_source_for_operation(op.type, t.indices, op.params)


def prepare_track(track, cfg):
    """
    Ensures that all track data are available for running the benchmark.

    :param track: A track that is about to be run.
    :param cfg: The config object.
    """

    def download(cfg, url, local_path, size_in_bytes):
        offline = cfg.opts("system", "offline.mode")
        file_exists = os.path.isfile(local_path)

        # ensure we only skip the download if the file size also matches our expectation
        if file_exists and os.path.getsize(local_path) == size_in_bytes:
            logger.info("[%s] already exists locally. Skipping download." % local_path)
            return False

        if not offline:
            try:
                io.ensure_dir(os.path.dirname(local_path))
                size_in_mb = round(convert.bytes_to_mb(size_in_bytes))
                # ensure output appears immediately
                console.info("Downloading data from [%s] (%s MB) to [%s] ... " % (url, size_in_mb, local_path),
                             end='', flush=True, logger=logger)
                net.download(url, local_path, size_in_bytes)
                console.println("[OK]")
            except urllib.error.URLError:
                logger.exception("Could not download [%s] to [%s]." % (url, local_path))

        # file must exist at this point -> verify
        if not os.path.isfile(local_path):
            if offline:
                raise exceptions.SystemSetupError(
                    "Cannot find %s. Please disable offline mode and retry again." % local_path)
            else:
                raise exceptions.SystemSetupError(
                    "Cannot download from %s to %s. Please verify that data are available at %s and "
                    "check your internet connection." % (url, local_path, url))

        actual_size = os.path.getsize(local_path)
        if actual_size != size_in_bytes:
            raise exceptions.DataError("[%s] is corrupt. Downloaded [%d] bytes but [%d] bytes are expected." %
                                       (local_path, actual_size, size_in_bytes))

        return True

    def decompress(data_set_path, expected_size_in_bytes):
        # we assume that track data are always compressed and try to decompress them before running the benchmark
        basename, extension = io.splitext(data_set_path)
        decompressed = False
        if not os.path.isfile(basename) or os.path.getsize(basename) != expected_size_in_bytes:
            decompressed = True
            console.info("Decompressing track data from [%s] to [%s] (resulting size: %.2f GB) ... " %
                         (data_set_path, basename, convert.bytes_to_gb(type.uncompressed_size_in_bytes)), end='', flush=True, logger=logger)
            io.decompress(data_set_path, io.dirname(data_set_path))
            console.println("[OK]")
            extracted_bytes = os.path.getsize(basename)
            if extracted_bytes != expected_size_in_bytes:
                raise exceptions.DataError("[%s] is corrupt. Extracted [%d] bytes but [%d] bytes are expected." %
                                           (basename, extracted_bytes, expected_size_in_bytes))
        return basename, decompressed

    for index in track.indices:
        for type in index.types:
            if type.document_archive:
                data_url = "%s/%s" % (track.source_root_url, os.path.basename(type.document_archive))
                download(cfg, data_url, type.document_archive, type.compressed_size_in_bytes)
                decompressed_file_path, was_decompressed = decompress(type.document_archive, type.uncompressed_size_in_bytes)
                # just rebuild the file every time for the time being. Later on, we might check the data file fingerprint to avoid it
                io.prepare_file_offset_table(decompressed_file_path)


class TrackRepository:
    """
    Manages track specifications.
    """

    def __init__(self, cfg, fetch=True):
        self.cfg = cfg
        self.name = cfg.opts("system", "track.repository")
        self.offline = cfg.opts("system", "offline.mode")
        # If no URL is found, we consider this a local only repo (but still require that it is a git repo)
        self.url = cfg.opts("tracks", "%s.url" % self.name, mandatory=False)
        self.remote = self.url is not None and self.url.strip() != ""
        root = cfg.opts("system", "root.dir")
        track_repositories = cfg.opts("benchmarks", "track.repository.dir")
        self.tracks_dir = "%s/%s/%s" % (root, track_repositories, self.name)
        if self.remote and not self.offline and fetch:
            # a normal git repo with a remote
            if not git.is_working_copy(self.tracks_dir):
                git.clone(src=self.tracks_dir, remote=self.url)
            else:
                git.fetch(src=self.tracks_dir)
        else:
            if not git.is_working_copy(self.tracks_dir):
                raise exceptions.SystemSetupError("[{src}] must be a git repository.\n\nPlease run:\ngit -C {src} init"
                                                  .format(src=self.tracks_dir))

    def track_names(self, distribution_version):
        self._update(distribution_version)
        return filter(lambda d: not d.startswith("."), next(os.walk(self.tracks_dir))[1])

    def track_dir(self, track_name):
        return "%s/%s" % (self.tracks_dir, track_name)

    def track_file(self, distribution_version, track_name):
        self._update(distribution_version)
        return "%s/track.json" % self.track_dir(track_name)

    def plugin_file(self, distribution_version, track_name):
        # TODO dm 71: We should  assume here that somebody else has already checked out the correct branch. Revisit when we distribute drivers
        #self._update(distribution_version)
        return "%s/track.py" % self.track_dir(track_name)

    def _update(self, distribution_version):
        try:
            if self.remote and not self.offline:
                branch = versions.best_match(git.branches(self.tracks_dir, remote=self.remote), distribution_version)
                if branch:
                    # Allow uncommitted changes iff we do not have to change the branch
                    logger.info(
                        "Checking out [%s] in [%s] for distribution version [%s]." % (branch, self.tracks_dir, distribution_version))
                    git.checkout(self.tracks_dir, branch=branch)
                    logger.info("Rebasing on [%s] in [%s] for distribution version [%s]." % (branch, self.tracks_dir, distribution_version))
                    try:
                        git.rebase(self.tracks_dir, branch=branch)
                    except exceptions.SupplyError:
                        logger.exception("Cannot rebase due to local changes in [%s]" % self.tracks_dir)
                        console.warn(
                            "Local changes in [%s] prevent track update from remote. Please commit your changes." % self.tracks_dir)
                    return
                else:
                    msg = "Could not find track data remotely for distribution version [%s]. " \
                          "Trying to find track data locally." % distribution_version
                    logger.warn(msg)
            branch = versions.best_match(git.branches(self.tracks_dir, remote=False), distribution_version)
            if branch:
                logger.info("Checking out [%s] in [%s] for distribution version [%s]." % (branch, self.tracks_dir, distribution_version))
                git.checkout(self.tracks_dir, branch=branch)
            else:
                raise exceptions.SystemSetupError("Cannot find track data for distribution version %s" % distribution_version)
        except exceptions.SupplyError:
            tb = sys.exc_info()[2]
            raise exceptions.DataError("Cannot update track data in [%s]." % self.tracks_dir).with_traceback(tb)


def render_template(loader, template_name, clock=time.Clock):
    env = jinja2.Environment(loader=loader)
    env.globals["now"] = clock.now()
    env.filters["days_ago"] = time.days_ago
    template = env.get_template(template_name)

    return template.render()


def render_template_from_file(template_file_name):
    return render_template(loader=jinja2.FileSystemLoader(io.dirname(template_file_name)), template_name=io.basename(template_file_name))


class TrackFileReader:
    """
    Creates a track from a track file.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        track_schema_file = "%s/resources/track-schema.json" % (self.cfg.opts("system", "rally.root"))
        self.track_schema = json.loads(open(track_schema_file).read())
        self.read_track = TrackSpecificationReader()

    def read(self, track_name, track_spec_file, mapping_dir, data_dir):
        """
        Reads a track file, verifies it against the JSON schema and if valid, creates a track.

        :param track_name: The name of the track.
        :param track_spec_file: The complete path to the track specification file.
        :param mapping_dir: The directory where the mapping files for this track are stored locally.
        :param data_dir: The directory where the data file for this track are stored locally.
        :return: A corresponding track instance if the track file is valid.
        """

        logger.info("Reading track specification file [%s]." % track_spec_file)
        try:
            rendered = render_template_from_file(track_spec_file)
            logger.info("Final rendered track for '%s': %s" % (track_spec_file, rendered))
            track_spec = json.loads(rendered)
        except (json.JSONDecodeError, jinja2.exceptions.TemplateError) as e:
            logger.exception("Could not load [%s]." % track_spec_file)
            raise TrackSyntaxError("Could not load '%s'" % track_spec_file, e)
        try:
            jsonschema.validate(track_spec, self.track_schema)
        except jsonschema.exceptions.ValidationError as ve:
            raise TrackSyntaxError(
                "Track '%s' is invalid.\n\nError details: %s\nInstance: %s\nPath: %s\nSchema path: %s"
                % (track_name, ve.message,
                   json.dumps(ve.instance, indent=4, sort_keys=True), ve.absolute_path, ve.absolute_schema_path))
        return self.read_track(track_name, track_spec, mapping_dir, data_dir)


class TrackPluginReader:
    """
    Loads track plugins
    """
    def __init__(self, runner_registry):
        self.runner_registry = runner_registry

    def __call__(self, track_plugin_file):
        loader = importlib.machinery.SourceFileLoader("track", track_plugin_file)
        module = types.ModuleType(loader.name)
        loader.exec_module(module)
        # every module needs to have a register() method
        module.register(self)

    def register_param_source(self, name, param_source):
        params.register_param_source_for_name(name, param_source)

    def register_runner(self, name, runner):
        self.runner_registry(name, runner)


class TrackSpecificationReader:
    """
    Creates a track instances based on its parsed JSON description.
    """

    def __init__(self):
        self.name = None

    def __call__(self, track_name, track_specification, mapping_dir, data_dir):
        self.name = track_name
        short_description = self._r(track_specification, ["meta", "short-description"])
        description = self._r(track_specification, ["meta", "description"])
        source_root_url = self._r(track_specification, ["meta", "data-url"])
        indices = [self._create_index(idx, mapping_dir, data_dir) for idx in self._r(track_specification, "indices")]
        challenges = self._create_challenges(track_specification)

        return track.Track(name=self.name, short_description=short_description, description=description,
                           source_root_url=source_root_url,
                           challenges=challenges, indices=indices)

    def _error(self, msg):
        raise TrackSyntaxError("Track '%s' is invalid. %s" % (self.name, msg))

    def _r(self, root, path, error_ctx=None, mandatory=True, default_value=None):
        if isinstance(path, str):
            path = [path]

        structure = root
        try:
            for k in path:
                structure = structure[k]
            return structure
        except KeyError:
            if mandatory:
                if error_ctx:
                    self._error("Mandatory element '%s' is missing in '%s'." % (".".join(path), error_ctx))
                else:
                    self._error("Mandatory element '%s' is missing." % ".".join(path))
            else:
                return default_value

    def _create_index(self, index_spec, mapping_dir, data_dir):
        index_name = self._r(index_spec, "name")
        types = [self._create_type(type_spec, mapping_dir, data_dir) for type_spec in self._r(index_spec, "types")]
        valid_document_data = False
        for type in types:
            if type.has_valid_document_data():
                valid_document_data = True
                break
        if not valid_document_data:
            console.warn("None of the types for index [%s] defines documents. Please check that you either don't want to index data or "
                         "parameter sources are defined for indexing." % index_name, logger=logger)

        return track.Index(name=index_name, types=types)

    def _create_type(self, type_spec, mapping_dir, data_dir):
        compressed_docs = self._r(type_spec, "documents", mandatory=False)
        if compressed_docs:
            document_archive = "%s/%s" % (data_dir, compressed_docs)
            document_file = "%s/%s" % (data_dir, io.splitext(compressed_docs)[0])
        else:
            document_archive = None
            document_file = None

        return track.Type(name=self._r(type_spec, "name"),
                          mapping_file="%s/%s" % (mapping_dir, self._r(type_spec, "mapping")),
                          document_file=document_file,
                          document_archive=document_archive,
                          number_of_documents=self._r(type_spec, "document-count", mandatory=False, default_value=0),
                          compressed_size_in_bytes=self._r(type_spec, "compressed-bytes", mandatory=False),
                          uncompressed_size_in_bytes=self._r(type_spec, "uncompressed-bytes", mandatory=False)
                          )

    def _create_challenges(self, track_spec):
        ops = self.parse_operations(self._r(track_spec, "operations"))
        challenges = []
        for challenge in self._r(track_spec, "challenges"):
            challenge_name = self._r(challenge, "name", error_ctx="challenges")
            challenge_description = self._r(challenge, "description", error_ctx=challenge_name)
            index_settings = self._r(challenge, "index-settings", error_ctx=challenge_name, mandatory=False)

            schedule = []

            for op in self._r(challenge, "schedule", error_ctx=challenge_name):
                if "parallel" in op:
                    task = self.parse_parallel(op["parallel"], ops, challenge_name)
                else:
                    task = self.parse_task(op, ops, challenge_name)
                schedule.append(task)

            challenges.append(track.Challenge(name=challenge_name,
                                              description=challenge_description,
                                              index_settings=index_settings,
                                              schedule=schedule))
        return challenges

    def parse_parallel(self, ops_spec, ops, challenge_name):
        default_warmup_iterations = self._r(ops_spec, "warmup-iterations", error_ctx="parallel", mandatory=False)
        default_iterations = self._r(ops_spec, "iterations", error_ctx="parallel", mandatory=False)
        clients = self._r(ops_spec, "clients", error_ctx="parallel", mandatory=False)

        # now descent to each operation
        tasks = []
        for task in self._r(ops_spec, "tasks", error_ctx="parallel"):
            tasks.append(self.parse_task(task, ops, challenge_name, default_warmup_iterations, default_iterations))
        return track.Parallel(tasks, clients)

    def parse_task(self, task_spec, ops, challenge_name, default_warmup_iterations=0, default_iterations=1):
        op_name = task_spec["operation"]
        if op_name not in ops:
            self._error("'schedule' for challenge '%s' contains a non-existing operation '%s'. "
                        "Please add an operation '%s' to the 'operations' block." % (challenge_name, op_name, op_name))
        return track.Task(operation=ops[op_name],
                          warmup_iterations=self._r(task_spec, "warmup-iterations", error_ctx=op_name, mandatory=False,
                                                    default_value=default_warmup_iterations),
                          warmup_time_period=self._r(task_spec, "warmup-time-period", error_ctx=op_name, mandatory=False),
                          iterations=self._r(task_spec, "iterations", error_ctx=op_name, mandatory=False, default_value=default_iterations),
                          clients=self._r(task_spec, "clients", error_ctx=op_name, mandatory=False, default_value=1),
                          target_throughput=self._r(task_spec, "target-throughput", error_ctx=op_name, mandatory=False))

    def parse_operations(self, ops_specs):
        # key = name, value = operation
        ops = {}
        for op_spec in ops_specs:
            op_name = self._r(op_spec, "name", error_ctx="operations")
            # Rally's core operations will still use enums then but we'll allow users to define arbitrary operations
            op_type_name = self._r(op_spec, "operation-type", error_ctx="operations")
            try:
                op_type = track.OperationType.from_hyphenated_string(op_type_name).name
                logger.debug("Using built-in operation type [%s] for operation [%s]." % (op_type, op_name))
            except KeyError:
                logger.info("Using user-provided operation type [%s] for operation [%s]." % (op_type_name, op_name))
                op_type = op_type_name
            param_source = self._r(op_spec, "param-source", error_ctx="operations", mandatory=False)
            try:
                ops[op_name] = track.Operation(name=op_name, operation_type=op_type, params=op_spec, param_source=param_source)
            except exceptions.InvalidSyntax as e:
                raise TrackSyntaxError("Invalid operation [%s]: %s" % (op_name, str(e)))
        return ops
