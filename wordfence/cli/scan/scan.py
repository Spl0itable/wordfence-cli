import sys
import signal
import os
from multiprocessing import parent_process
from contextlib import nullcontext
from logging import DEBUG

from wordfence import scanning, api
from wordfence.scanning import filtering
from wordfence.util import caching
from wordfence.util import updater
from wordfence.util.io import StreamReader
from wordfence.intel.signatures import SignatureSet
from wordfence.logging import log
from .reporting import Report, ReportFormat


class ScanCommand:

    CACHEABLE_TYPES = {
            'wordfence.intel.signatures.SignatureSet',
            'wordfence.intel.signatures.CommonString',
            'wordfence.intel.signatures.Signature'
        }

    def __init__(self, config):
        self.config = config
        self.cache = self._initialize_cache()
        self.license = None
        self.cacheable_signatures = None

    def _get_license(self) -> api.licensing.License:
        if self.license is None:
            if self.config.license is None:
                raise api.licensing.LicenseRequiredException()
            self.license = api.licensing.License(self.config.license)
        return self.license

    def _initialize_cache(self) -> caching.Cache:
        if self.config.cache:
            try:
                return caching.CacheDirectory(
                        self.config.cache_directory,
                        self.CACHEABLE_TYPES
                    )
            except caching.CacheException:
                # TODO: Should cache failures trigger some kind of notice?
                pass
        return caching.RuntimeCache()

    def filter_signatures(self, signatures: SignatureSet) -> None:
        if self.config.exclude_signatures is None:
            return
        for identifier in self.config.exclude_signatures:
            signatures.remove_signature(identifier)

    def _get_signatures(self) -> SignatureSet:
        if self.cacheable_signatures is None:
            def fetch_signatures() -> SignatureSet:
                noc1_client = api.noc1.Client(
                        self._get_license(),
                        base_url=self.config.noc1_url
                    )
                return noc1_client.get_malware_signatures()
            self.cacheable_signatures = caching.Cacheable(
                    'signatures',
                    fetch_signatures,
                    86400  # Cache signatures for 24 hours
                )
        signatures = self.cacheable_signatures.get(self.cache)
        self.filter_signatures(signatures)
        return signatures

    def _should_read_stdin(self) -> bool:
        if sys.stdin is None:
            return False
        if self.config.read_stdin is None:
            return not sys.stdin.isatty()
        else:
            return self.config.read_stdin

    def _get_file_list_separator(self) -> str:
        if isinstance(self.config.file_list_separator, bytes):
            return self.config.file_list_separator.decode('utf-8')
        return self.config.file_list_separator

    def _initialize_file_filter(self) -> filtering.FileFilter:
        filter = filtering.FileFilter()
        has_include_overrides = False
        if self.config.include_files is not None:
            has_include_overrides = True
            for name in self.config.include_files:
                filter.add(filtering.filter_filename(name))
        if self.config.include_files_pattern is not None:
            has_include_overrides = True
            for pattern in self.config.include_files_pattern:
                filter.add(filtering.filter_pattern(pattern))
        if self.config.exclude_files is not None:
            for name in self.config.exclude_files:
                filter.add(filtering.filter_filename(name), False)
        if self.config.exclude_files_pattern is not None:
            for pattern in self.config.exclude_files_pattern:
                filter.add(filtering.filter_pattern(pattern), False)
        if not has_include_overrides:
            filter.add(filtering.filter_php)
            filter.add(filtering.filter_html)
            filter.add(filtering.filter_js)
            if self.config.images:
                filter.add(filtering.filter_pattern(self.config.images))
        return filter

    def execute(self) -> int:
        updater.Version.check(self.cache)
        paths = set()
        for argument in self.config.trailing_arguments:
            paths.add(argument)
        options = scanning.scanner.Options(
                paths=paths,
                threads=int(self.config.threads),
                signatures=self._get_signatures(),
                chunk_size=self.config.chunk_size,
                max_file_size=self.config.max_file_size,
                file_filter=self._initialize_file_filter()
            )
        if self._should_read_stdin():
            options.path_source = StreamReader(
                    sys.stdin,
                    self._get_file_list_separator()
                )

        with open(self.config.output_path, 'w') if self.config.output_path \
                is not None else nullcontext() as output_file:
            output_format = ReportFormat(self.config.output_format)
            output_columns = self.config.output_columns.split(',')
            report = Report(output_format, output_columns, options.signatures)
            if self.config.output and sys.stdout is not None:
                report.add_target(sys.stdout)
            if output_file is not None:
                report.add_target(output_file)
            if self.config.output_headers:
                report.write_headers()
            scanner = scanning.scanner.Scanner(options)
            scanner.scan(lambda result: report.add_result(result))
        return 0


def handle_repeated_interrupt(signal_number: int, stack) -> None:
    if parent_process() is None:
        log.warning('Scan command terminating immediately...')
    os._exit(130)


def handle_interrupt(signal_number: int, stack) -> None:
    if parent_process() is None:
        log.info('Scan command interrupted, stopping...')
    signal.signal(signal.SIGINT, handle_repeated_interrupt)
    sys.exit(130)


signal.signal(signal.SIGINT, handle_interrupt)


def main(config) -> int:
    command = None
    try:
        if config.verbose:
            log.setLevel(DEBUG)
        command = ScanCommand(config)
        command.execute()
        return 0
    except api.licensing.LicenseRequiredException:
        log.error('A valid Wordfence CLI license is required')  # TODO: stderr
        return 1
    except BaseException as exception:
        raise exception
        log.error(f'Error: {exception}')
        return 1