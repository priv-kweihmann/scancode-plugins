#!/usr/bin/env python3
# Copyright (c) 2020 nexB Inc.

"""
Utility to keep linux and macOS prebuilt ScanCode toolkit plugins up to date.
Note that homebrew
"""

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys

from distutils.dir_util import copy_tree

import shared_utils

REQUEST_TIMEOUT = 60

TRACE = False
TRACE_DEEP = False

# homebrew uses version names and not numbers.
# See https://en.wikipedia.org/wiki/MacOS_version_history#Releases
MACOS_VERSIONS = {
    '10.6': 'snow_leopard',
    '10.7': 'lion',
    '10.8': 'mountain_lion',
    '10.9': 'mavericks',
    '10.10': 'yosemite',
    '10.11': 'el_capitan',
    '10.12': 'sierra',
    '10.13': 'high_sierra',
    '10.14': 'mojave',
    '10.15': 'catalina',
    '11.1': 'big_sur',
}

DARWIN_VERSIONS = {
    '10.6': '10',
    '10.7': '11',
    '10.8': '12',
    '10.9': '13',
    '10.10': '14',
    '10.11': '15',
    '10.12': '16',
    '10.13': '17',
    '10.14': '18',
    '10.15': '19',
    '11.1': '20',
}

CURRENT_MACOSX_VERSION = 'mojave'

"""
https://github.com/Homebrew/formulae.brew.sh/blob/b578ad73a21ce8078e68c28d2a8a94afc0f31654/_config.yml#L43
homebrew-core
linuxbrew-core
homebrew-cask
"""


class Repository:
    """
    A repository (either 32 or 64 bits) and its collection of packages.
    """

    def __init__(self, name, db_url, formula_base_url):
        self.name = name
        self.db_url = db_url
        self.formula_base_url = formula_base_url
        # a collection of {binary_package_name: BinaryPackage object}
        self.packages = {}

    def update_packages_index(self, cache_dir):
        """
        Populate BinaryPackage and SourcePackage in this repo.
        Caches the data for the duration of the session.
        """
        if self.packages:
            return self.packages
        print('Loading Repo from %r' % self.db_url)
        packages = self.packages = {}

        index_loc = shared_utils.fetch_file(
            url=self.db_url,
            dir_location=cache_dir,
            file_name=f'formula-{self.name}.json',
            force=True)

        with open(index_loc) as il:
            items = json.load(il)

        for item in items:
            try:
                binary = BinaryPackage.from_index(item, repo=self)
                packages[binary.name] = binary
            except:
                if TRACE_DEEP:
                    print('Skipping incomplete package: {name}'.format(**item))
        return packages


OSARCHES = [
    'big_sur', 'catalina', 'mojave', 'high_sierra', 'sierra', 'el_capitan',
    'mavericks', 'yosemite',
    'x86_64_linux',
]

REPOSITORIES = {
    'x86_64_linux': Repository(
        name='linuxbrew',
        db_url='https://formulae.brew.sh/api/formula-linux.json',
        formula_base_url='https://raw.githubusercontent.com/Homebrew/linuxbrew-core/master/Formula/{}.rb'),
    # mojave is the oldest version available on homebrew
    CURRENT_MACOSX_VERSION: Repository(
        name='homebrew',
        db_url='https://formulae.brew.sh/api/formula.json',
        formula_base_url='https://raw.githubusercontent.com/Homebrew/homebrew-core/master/Formula/{}.rb'),
}


class Download:
    """
    Represent a source, patch or binary download.
    """

    def __init__(self, url, file_name=None, sha256=None):
        self.url = url.strip('/')
        if not file_name:
            _, _, file_name = self.url.rpartition('/')
        self.file_name = file_name
        self.sha256 = sha256
        self.fetched_location = None

    def __repr__(self, *args, **kwargs):
        return f'Download({self.url}, {self.file_name}, {self.sha256})'

    @classmethod
    def from_index(cls, url, tag=None, revision=None, sha256=None, **kwargs):
        """
        The index contains these three fields for a URL:
            "url": "https://github.com/coccinelle/coccinelle.git",
            "tag": "1.0.8",
            "revision": "d678c34afc0cfb479ad34f2225c57b1b8d3ebeae"
        """
        url = url.strip('/')
        if not tag and not revision:
            _, _, file_name = url.rpartition('/')
            return cls(url=url, file_name=file_name, sha256=sha256)

        # a github URL
        assert url.startswith('https://github.com'), f'Invalid {url}'
        if not tag and not revision:
            return

        if url.endswith('.git'):
            url, _, _ = url.rpartition('.git')

        # prefer revision over tag
        commitish = revision or tag
        download_url = f'{url}/archive/{commitish}.tar.gz'
        _, _, ghrepo_name = url.rpartition('/')
        file_name = f'{ghrepo_name}-{commitish}.tar.gz'

        return Download(
            url=download_url,
            file_name=file_name,
            sha256=sha256,
        )

    def fetch(self, dir_location, force=False, verify=True):
        """
        Fetch this download and save it in `dir_location`.
        Return the `location` where the file is saved.
        If `force` is False, do not refetch if already fetched.
        """
        self.fetched_location = shared_utils.fetch_file(
            url=self.url,
            dir_location=dir_location,
            file_name=self.file_name,
            force=force,
        )

        if verify:
            shared_utils.verify(self.fetched_location, self.sha256)

        return self.fetched_location


class BinaryPackage:

    def __init__(
        self,
        name,
        version,
        revision,
        download_urls,
        formula_download_url,
        source_download_urls,
        depends):

        self.name = name
        self.version = version
        self.revision = revision
        # mapping of {osarch: Download}
        self.download_urls = download_urls
        # direct fetch of the ruby code of a formula
        self.formula_download_url = formula_download_url
        # list of Download: archive, patches, etc
        self.source_download_urls = source_download_urls
        self.depends = depends
        self.fullversion = self.version
        if self.revision:
            self.fullversion += '_' + self.revision
        self.fqname = f'{self.name}@{self.fullversion}'

    def __repr__(self) -> str:
        return f'BinaryPackage({self.fqname})'

    def add_formula_source_download_urls(self, location):
        """
        Add source_download_urls found in the Ruby formula file at `location`.
        These would typically be patches and simialr as the sources should
        already have been taken care of.
        """
        known_urls = set(d.url for d in self.source_download_urls)
        for url in get_formula_source_urls(location):
            if url not in known_urls:
                dnl = Download(url)
                if not dnl:
                    # this can happen for bare github URL when we have no tag or commit
                    continue

                self.source_download_urls.append(dnl)

    @classmethod
    def from_index(cls, item, repo):
        """
        Return a BinaryPackage built from an index entry.
            {
                "name": "clamav",
                "versions": {
                  "stable": "0.102.2",
            ...
                },
                "urls": {
                  "stable": {
                    "url": "https://www.clamav.net/downloads/production/clamav-0.102.2.tar.gz",
                    "tag": null,
                    "revision": null
                  }
                },
                "revision": 0,
                "bottle": {
                  "stable": {
                    "rebuild": 0,
            ...
                    "files": {
                      "catalina": {
                        "url": "https://homebrew.bintray.com/bottles/clamav-0.102.2.catalina.bottle.tar.gz",
                        "sha256": "544f511ddd1c68b88a93f017617c968a4e5d34fc6a010af15e047a76c5b16a9f"
                      },
                      "mojave": {
                        "url": "https://homebrew.bintray.com/bottles/clamav-0.102.2.mojave.bottle.tar.gz",
                        "sha256": "a92959f8a348642739db5e023e4302809c8272da1bea75336635267e449aacdf"
                      },
                    }
                  }
                },
            ....
                "dependencies": [
                  "json-c",
                  "openssl@1.1",
                  "pcre",
                  "yara"
                ],
            ....

        """
        name = item['name']
        version = item['versions']['stable']
        revision = item['revision']
        if revision == 0:
            revision = ''
        revision = str(revision)

        formula_download_url = Download(url=repo.formula_base_url.format(name))
        source_url = item['urls']['stable']
        sdu = Download.from_index(**source_url)
        source_download_urls = [sdu]

        download_urls = {}
        for osarch, durl in item['bottle']['stable']['files'].items():
            if repo.name not in durl['url']:
                # in linuxbrew, we have incorrect URLS for homebrew packages
                continue
            archdu = Download(url=durl['url'], sha256=durl['sha256'])
            download_urls[osarch] = archdu

        depends = []
        for dep in item['dependencies']:
            dname, _, dversion = dep.partition('@')
            depends.append((dname, dversion,))

        bp = BinaryPackage(
            name=name,
            version=version,
            revision=revision,
            download_urls=download_urls,
            formula_download_url=formula_download_url,
            source_download_urls=source_download_urls,
            depends=depends,
        )

        if TRACE_DEEP:
            print(f'for: {bp}')
            for arch, dnl  in download_urls.items():
                print('    ', arch, dnl)
        return bp

    def get_all_dependents(self, binary_packages, ignore_deps=()):
        """
        Yield all the recursive deps of this package given a packages mapping
        of {name: package}
        """
        for dep_name, _dep_req in self.depends:
            if ignore_deps and dep_name in ignore_deps:
                continue

            try:
                depp = binary_packages[dep_name]
            except KeyError:
                depp = binary_packages[dep_name + '-git']

            yield depp

            for subdep in depp.get_all_dependents(binary_packages, ignore_deps):
                yield subdep

    def get_unique_dependents(self, binary_packages, ignore_deps=()):
        """
        Return a list of unique package deps of this package given a
        packages mapping of {name: package}
        """
        unique = {}
        for dep in self.get_all_dependents(binary_packages, ignore_deps):
            if dep.name not in unique:
                unique[dep.name] = dep
        return list(unique.values())


def get_formula_source_urls(location):
    """
    Yield URLs extracted from the Ruby formula file at location.
    """
    good_extensions = (
        '.tar.gz',
        '.tar.xz',
        '.tar.bz2',
        '.zip',
    )
    with open(location) as formula:
        for line in formula:
            line = line .strip()
            if line.startswith('url '):
                _, _, url = line.partition('url "')
                url = url.strip(' "')
                if url.endswith(good_extensions):
                    yield  url


def install_files(extracted_dir, install_dir, package_name, package_fullversion, copies=None):
    """
    Install libraries and licenses from the extracted_dir
    - lib dir files are installed in install_dir/lib
    - share/licenses dir files are installed in install_dir/licenses
    - share/docs dir files are installed in install_dir/docs
    """
    # map of src to dst
    default_copies = {
        # base
        'lib': 'lib',
        'bin': 'bin',

        # doc and licenses
        'share': 'licenses',
    }

    copies = copies or default_copies

    if TRACE: print('    Installing with:', copies)

    for src, dst in copies.items():
        isdir = dst.endswith('/')
        src = os.path.join(extracted_dir, src)
        dst = os.path.join(install_dir, dst)
        if os.path.exists(src):
            if TRACE: print('      copying:', src, dst)
            if os.path.isdir(src):
                copy_tree(src, dst)
            else:
                parent = os.path.dirname(dst)
                os.makedirs(parent, exist_ok=True)
                if isdir:
                    os.makedirs(dst, exist_ok=True)
                shutil.copy2(src, dst)


def patchelf(*args):
    """
    Run patchelf with the provided `args` arguments to patch an ELF file. The
    primary use is to set a proper RPATH to load needed shared objects and avoid
    the dependency on LD_LIBRARY_PATH.
    """
    cmdline = ['patchelf'] + list(args)
    subprocess.check_call(cmdline)


def patchmacho(exe_path):
    """
    Patch a Mach-O file by rewriting loader path using macholib for the
    provided `exe_path`. The primary use is to set a proper @loader_path to load
    needed dylib shared objects and avoid the dependency on DYLD_LIBRARY_PATH
    and similar.

    Replace header such as:
        @@HOMEBREW_PREFIX@@/opt/xz/lib/liblzma.5.dylib
    with:
        @loader_path/opt/zstd/lib/libzstd.1.dylib
    Leave other unchanged such as:
        /usr/lib/libSystem.B.dylib

    TODO: what about the "SONAME" proper?
        @@HOMEBREW_PREFIX@@/opt/libarchive/lib/libarchive.13.dylib
    """

    def get_updated_loader_path(loader_path):
        """
        MachO.rewriteLoadCommands `changefunc` to only change HOMEBREW-
        prefixed paths and leave any system library path unchanged.
        """
        if '@@HOMEBREW_PREFIX@@' in loader_path:
            # make this @loader_path-relative
            _, _, dylib_name = loader_path.rpartition('/')
            return f'@loader_path/{dylib_name}'

    from macholib.MachO import MachO
    exe = MachO(exe_path)
    exe.rewriteLoadCommands(changefunc=get_updated_loader_path)

    # from macholib.MachOStandalone
    with open(exe_path, 'rb+') as macho:
        for _header in exe.headers:
            macho.seek(0)
            exe.write(macho)
        macho.seek(0, 2)
        macho.flush()


def apply_fixes(fixes):
    """
    Apply a list of `fixes` as (fixer, args) to an executable file (provided in the args).
    """
    fixers = {
        'patchelf': patchelf,
        'patchmacho': patchmacho,
    }
    for fix in fixes:
        fixer = fix[0]
        args = fix[1:]
        fixer = fixers[fixer]
        fixer(*args)


def check_installed_files(install_dir, copies, package):
    """
    Verifies that all the `copies` operations for Package `package` took place with
    all files present in `install_dir`
    """
    missing = []
    for src, dst in copies.items():
        src_isdir = src.endswith('/')
        dst_isdir = dst.endswith('/')
        dst_loc = os.path.join(install_dir, dst)

        if dst_isdir and not src_isdir:
            # file to dir
            filename = os.path.basename(src)
            dst_loc = os.path.join(dst_loc, filename)
            if not os.path.exists(dst_loc):
                missing.append(dst_loc)
            continue
        if dst_isdir and src_isdir:
            # dir to dir
            if not os.path.exists(dst_loc):
                missing.append(dst_loc)
            else:
                if not os.listdir(dst_loc):
                    missing.append(dst_loc)
            continue
        if not dst_isdir:
            # file to file
            if not os.path.exists(dst_loc):
                missing.append(dst_loc)
            continue

        if src_isdir and not dst_isdir:
            # dir to file: illegal
            raise Exception(f'Illegal copy from: {src} to {dst}.')
            continue

    if missing:
        missing = '\n'.join(missing)
        raise Exception(f'These files were not installed for {package}:\n{missing}')


def update_package(
    name,
    osarch,
    fullversion=None,
    cache_dir=None,
    install_dir=None,
    ignore_deps=(),
    copies=None,
    deletes=(),
    fixes=(),
):
    """
    Fetch a `package` with `name` for `osarch` and optional `fullversion` and
    save its sources and binaries as well as its full dependency tree sources
    and binaries in the `cache_dir` directory, ignoring `ignore_deps` list of
    dependencies. Then delete the list of paths under `install_dir` in
    `deletes`. Then install in `install_dir` using `copies` {from:to} copy
    operations. Finally copy all the sources to `thirdparty_dir`
    """
    # Apply presets
    presets = PRESETS.get((name, osarch,), {})
    copies = copies or presets.get('copies', {})
    ignore_deps = ignore_deps or presets.get('ignore_deps', [])
    fullversion = fullversion or presets.get('fullversion')
    install_dir = install_dir or presets['install_dir']
    deletes = deletes or presets.get('deletes', [])
    fixes = fixes or presets.get('fixes', [])

    # used for sources redistribution
    base_dir = presets['base_dir']
    thirdparty_dir = presets['thirdparty_dir']
    source_plugins_dir = presets['source_plugins_dir']

    for deletable in deletes:
        deletable = os.path.join(install_dir, deletable)
        if not os.path.exists(deletable):
            continue
        if os.path.isdir(deletable):
            shutil.rmtree(deletable, ignore_errors=False)
        else:
            os.remove(deletable)

    repository = REPOSITORIES[osarch]
    binary_packages = repository.update_packages_index(cache_dir=cache_dir)

    extracted_locations = []

    root_package = binary_packages[name]

    if fullversion and fullversion != root_package.fullversion:
        raise Exception(
            f'Incorrect version for {root_package.name}: '
            '{root_package.fullversion} vs. {fullversion}',
        )

    if not cache_dir:
        cache_dir = os.path.dirname(__file__)
    os.makedirs(cache_dir, exist_ok=True)

    bin_cache_dir = os.path.join(cache_dir, 'bin')
    os.makedirs(bin_cache_dir, exist_ok=True)

    src_cache_dir = os.path.join(cache_dir, 'src')
    os.makedirs(src_cache_dir, exist_ok=True)

    # create AND cleanup
    os.makedirs(thirdparty_dir, exist_ok=True)
    for srcf in os.listdir(thirdparty_dir):
        os.remove(os.path.join(thirdparty_dir, srcf))

    # create AND cleanup these too:
    base_dir_name = os.path.basename(base_dir)
    saved_sources_dir = os.path.join(source_plugins_dir, base_dir_name)
    if os.path.exists(saved_sources_dir):
        shutil.rmtree(saved_sources_dir, ignore_errors=False)

    extracted_to = process_package(
        package=root_package,
        osarch=osarch,
        install_dir=install_dir,
        thirdparty_dir=thirdparty_dir,
        copies=copies,
        bin_cache_dir=bin_cache_dir,
        src_cache_dir=src_cache_dir,
    )

    extracted_locations.append(extracted_to)

    print('Fetching deps for: {}, ignoring deps: {}'.format(
        root_package.name,
        ', '.join(ignore_deps)))

    for dependency in root_package.get_unique_dependents(
            binary_packages=binary_packages,
            ignore_deps=ignore_deps):

        extracted_to = process_package(
            package=dependency,
            osarch=osarch,
            install_dir=install_dir,
            thirdparty_dir=thirdparty_dir,
            copies=copies,
            bin_cache_dir=bin_cache_dir,
            src_cache_dir=src_cache_dir,
        )
        extracted_locations.append(extracted_to)

    check_installed_files(install_dir, copies, root_package)

    if fixes:
        with pushd(install_dir):
            apply_fixes(fixes)

    # cleanup after thyself, removing extracted locations
    for exloc in extracted_locations:
        if os.path.exists(exloc):
            if os.path.isdir(exloc):
                shutil.rmtree(exloc, False)
            else:
                os.remove(exloc)

    # finally make a copy of each plugins with their sources on our "sdist"
    copy_tree(base_dir, saved_sources_dir)


@contextlib.contextmanager
def pushd(path):
    """
    Context manager to change the current working directory to `path`.
    """
    original_cwd = os.getcwd()
    try:
        os.chdir(path)
        yield os.getcwd()
    finally:
        os.chdir(original_cwd)


def process_package(
    package,
    osarch,
    install_dir,
    thirdparty_dir,
    copies,
    bin_cache_dir,
    src_cache_dir,
):
    """
    Fetch sources and binaries and install files for package in plugin.
    """
    print(f'Fetching package: {package} for install in: {install_dir}')

    # fetch the binary for the requested osarch
    package_binary_download = package.download_urls[osarch]
    fetched_binary_loc = package_binary_download.fetch(dir_location=bin_cache_dir)
    extracted_dir = shared_utils.extract_in_place(fetched_binary_loc)

    # fetch the upstream formula and collect extra sources/patches:
    # formula_loc = package.formula_download_url.fetch(dir_location=src_cache_dir)

    # collect the actual formula(s) used for the build (which may be older than upstream)
    brew_dir = os.path.join(extracted_dir, package.name, package.fullversion, '.brew')
    for brew_formula in os.listdir(brew_dir):
        brew_formula_loc = os.path.join(brew_dir, brew_formula)
        shutil.copy2(brew_formula_loc, src_cache_dir)
        package.add_formula_source_download_urls(brew_formula_loc)

        # save also in the plugin thirdparty with an ABOUT file
        shutil.copy2(brew_formula_loc, thirdparty_dir)
        shared_utils.create_about_file(
            about_resource=brew_formula,
            name=package.name,
            version=package.fullversion,
            download_url=package_binary_download.url,
            target_directory=thirdparty_dir,
            notes='This is a brew formula used to create this package.'
        )

    # fetch all sources
    for src_download in package.source_download_urls:
        fetched_src_location = src_download.fetch(dir_location=src_cache_dir)

        # save also in the plugin thirdparty with an ABOUT file
        shutil.copy2(fetched_src_location, thirdparty_dir)
        shared_utils.create_about_file(
            about_resource=os.path.basename(fetched_src_location),
            name=package.name,
            version=package.fullversion,
            download_url=src_download.url,
            target_directory=thirdparty_dir,
            notes='This is a source archive or patch used to create this package with brew.'
        )

    # install the binary
    install_files(
        extracted_dir=extracted_dir,
        install_dir=install_dir,
        package_name=package.name,
        package_fullversion=package.fullversion,
        copies=copies,
    )
    return extracted_dir


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('-p', '--package', type=str,
        help='Package name to fetch')
    parser.add_argument('-v', '--fullversion', type=str, default=None,
        help='Package fullversion ')
    parser.add_argument('--osarch', type=str,
        choices=OSARCHES,
        help='OS/Arch to use for the selected package ')
    parser.add_argument('--cache-dir', type=str,
        help='Target directory where archives are fetched')
    parser.add_argument('--install-dir', type=str,
        help='Install directory where archive files are copied')
    parser.add_argument('--ignore-deps', type=str, action='append',
        help='Ignore a dependent package with this name. Repeat for more ignores.')
    parser.add_argument('--copies', type=str, action='append',
        help='Copy this extra file or directory from the binary package to the '
             'install directory (such as in foo=bar/data). Repeat for more copies.')
    parser.add_argument('--deletes', type=str, action='append',
        help='Delete this path before installing. Repeat for more paths.')
    parser.add_argument('--build-all', action='store_true',
        help='Build all default packages.')

    args = parser.parse_args()
    name = args.package
    fullversion = args.fullversion
    osarch = args.osarch
    copies = args.copies or {}
    if copies:
        copies = dict(op.split('=') for op in copies)

    ignore_deps = args.ignore_deps or []
    install_dir = args.install_dir or None
    cache_dir = args.cache_dir or None
    deletes = args.deletes  or []

    if TRACE_DEEP:
        print('name:', name)
        print('fullversion:', fullversion)
        print('install_dir:', install_dir)
        print('ignore_deps:', ignore_deps)
        print('copies:', copies)
        print('deletes:', deletes)

    if args.build_all:
        cache_dir = cache_dir or 'src-homebrew'
        update_package(name='libarchive', osarch='x86_64_linux', cache_dir=cache_dir)
        update_package(name='p7zip', osarch='x86_64_linux', cache_dir=cache_dir)
        update_package(name='libmagic', osarch='x86_64_linux', cache_dir=cache_dir)
        update_package(name='libarchive', osarch=CURRENT_MACOSX_VERSION, cache_dir=cache_dir)
        update_package(name='p7zip', osarch=CURRENT_MACOSX_VERSION, cache_dir=cache_dir)
        update_package(name='libmagic', osarch=CURRENT_MACOSX_VERSION, cache_dir=cache_dir)

    else:

        update_package(
            name=name,
            fullversion=fullversion,
            osarch=osarch,
            cache_dir=cache_dir,
            install_dir=install_dir,
            ignore_deps=ignore_deps,
            copies=copies,
            deletes=deletes,
        )


PRESETS = {
    # latest https://libarchive.org/downloads/libarchive-3.4.3.tar.gz
    ('libarchive', 'x86_64_linux'): {
        'fullversion': '3.4.3',
        'ignore_deps': [],
        'deletes': ['licenses', 'lib'],
        'install_dir': 'builtins/extractcode_libarchive-linux/src/extractcode_libarchive',
        'thirdparty_dir': 'builtins/extractcode_libarchive-linux/thirdparty',

        'base_dir': 'builtins/extractcode_libarchive-linux',
        'source_plugins_dir': 'builtins/extractcode_libarchive-sources',

        'fixes': [
            ('patchelf', '--set-soname', 'libarchive.so', 'lib/libarchive.so'),
            ('patchelf', '--set-rpath', '$ORIGIN/.', 'lib/libarchive.so'),

            ('patchelf', '--replace-needed', 'libb2.so.1'   , 'libb2-la343.so.1'   , 'lib/libarchive.so'),
            ('patchelf', '--replace-needed', 'libbsd.so.0'  , 'libbsd-la343.so.0'  , 'lib/libarchive.so'),
            ('patchelf', '--replace-needed', 'libbz2.so.1.0', 'libbz2-la343.so.1.0', 'lib/libarchive.so'),
            ('patchelf', '--replace-needed', 'libexpat.so.1', 'libexpat-la343.so.1', 'lib/libarchive.so'),
            ('patchelf', '--replace-needed', 'liblz4.so.1'  , 'liblz4-la343.so.1'  , 'lib/libarchive.so'),
            ('patchelf', '--replace-needed', 'liblzma.so.5' , 'liblzma-la343.so.5' , 'lib/libarchive.so'),
            ('patchelf', '--replace-needed', 'libz.so.1'    , 'libz-la343.so.1'    , 'lib/libarchive.so'),
            ('patchelf', '--replace-needed', 'libzstd.so.1' , 'libzstd-la343.so.1' , 'lib/libarchive.so'),

            ('patchelf', '--set-rpath', '$ORIGIN/.', 'lib/libb2-la343.so.1'),
            ('patchelf', '--set-rpath', '$ORIGIN/.', 'lib/libbsd-la343.so.0'),
            ('patchelf', '--set-rpath', '$ORIGIN/.', 'lib/libbz2-la343.so.1.0'),
            ('patchelf', '--set-rpath', '$ORIGIN/.', 'lib/libexpat-la343.so.1'),
            ('patchelf', '--set-rpath', '$ORIGIN/.', 'lib/liblz4-la343.so.1'),
            ('patchelf', '--set-rpath', '$ORIGIN/.', 'lib/liblzma-la343.so.5'),
            ('patchelf', '--set-rpath', '$ORIGIN/.', 'lib/libz-la343.so.1'),
            ('patchelf', '--set-rpath', '$ORIGIN/.', 'lib/libzstd-la343.so.1'),

            ('patchelf', '--set-soname', 'libb2-la343.so.1'   , 'lib/libb2-la343.so.1'),
            ('patchelf', '--set-soname', 'libbsd-la343.so.0'  , 'lib/libbsd-la343.so.0'),
            ('patchelf', '--set-soname', 'libbz2-la343.so.1.0', 'lib/libbz2-la343.so.1.0'),
            ('patchelf', '--set-soname', 'libexpat-la343.so.1', 'lib/libexpat-la343.so.1'),
            ('patchelf', '--set-soname', 'liblz4-la343.so.1'  , 'lib/liblz4-la343.so.1'),
            ('patchelf', '--set-soname', 'liblzma-la343.so.5' , 'lib/liblzma-la343.so.5'),
            ('patchelf', '--set-soname', 'libz-la343.so.1'    , 'lib/libz-la343.so.1'),
            ('patchelf', '--set-soname', 'libzstd-la343.so.1' , 'lib/libzstd-la343.so.1'),

        ],
        'copies': {
            'libarchive/3.4.3/lib/libarchive.so': 'lib/',
            'libarchive/3.4.3/INSTALL_RECEIPT.json': 'licenses/libarchive/',
            'libarchive/3.4.3/COPYING': 'licenses/libarchive/',
            'libarchive/3.4.3/README.md': 'licenses/libarchive/',

            'bzip2/1.0.8/lib/libbz2.so.1.0': 'lib/libbz2-la343.so.1.0',
            'bzip2/1.0.8/INSTALL_RECEIPT.json': 'licenses/bzip2/',
            'bzip2/1.0.8/LICENSE': 'licenses/bzip2/',
            'bzip2/1.0.8/README': 'licenses/bzip2/',
            'bzip2/1.0.8/CHANGES': 'licenses/bzip2/',

            'expat/2.2.10/lib/libexpat.so.1': 'lib/libexpat-la343.so.1',
            'expat/2.2.10/INSTALL_RECEIPT.json': 'licenses/expat/',
            'expat/2.2.10/COPYING': 'licenses/expat/',
            'expat/2.2.10/README.md': 'licenses/expat/',
            'expat/2.2.10/AUTHORS': 'licenses/expat/',
            'expat/2.2.10/Changes': 'licenses/expat/',
            'expat/2.2.10/share/doc/expat/changelog': 'licenses/expat/',

            'libb2/0.98.1/lib/libb2.so.1': 'lib/libb2-la343.so.1',
            'libb2/0.98.1/INSTALL_RECEIPT.json': 'licenses/libb2/',
            'libb2/0.98.1/COPYING': 'licenses/libb2/',

            'libbsd/0.10.0/lib/libbsd.so.0': 'lib/libbsd-la343.so.0',
            'libbsd/0.10.0/INSTALL_RECEIPT.json': 'licenses/libbsd/',
            'libbsd/0.10.0/COPYING': 'licenses/libbsd/',
            'libbsd/0.10.0/README': 'licenses/libbsd/',
            'libbsd/0.10.0/ChangeLog': 'licenses/libbsd/',

            'lz4/1.9.3/lib/liblz4.so.1': 'lib/liblz4-la343.so.1',
            'lz4/1.9.3/INSTALL_RECEIPT.json': 'licenses/lz4/',
            'lz4/1.9.3/LICENSE': 'licenses/lz4/',
            'lz4/1.9.3/README.md': 'licenses/lz4/',
            'lz4/1.9.3/include/lz4frame_static.h': 'licenses/lz4/lz4.LICENSE',

            'xz/5.2.5/lib/liblzma.so.5': 'lib/liblzma-la343.so.5',
            'xz/5.2.5/INSTALL_RECEIPT.json': 'licenses/xz/',
            'xz/5.2.5/COPYING': 'licenses/xz/',
            'xz/5.2.5/README': 'licenses/xz/',
            'xz/5.2.5/AUTHORS': 'licenses/xz/',
            'xz/5.2.5/share/doc/xz/THANKS': 'licenses/xz/',
            'xz/5.2.5/ChangeLog': 'licenses/xz/',

            'zlib/1.2.11/lib/libz.so.1': 'lib/libz-la343.so.1',
            'zlib/1.2.11/INSTALL_RECEIPT.json': 'licenses/zlib/',
            'zlib/1.2.11/README': 'licenses/zlib/',
            'zlib/1.2.11/ChangeLog': 'licenses/zlib/',

            'zstd/1.4.7/lib/libzstd.so.1': 'lib/libzstd-la343.so.1',
            'zstd/1.4.7/INSTALL_RECEIPT.json': 'licenses/zstd/',
            'zstd/1.4.7/COPYING': 'licenses/zstd/',
            'zstd/1.4.7/README.md': 'licenses/zstd/',
            'zstd/1.4.7/LICENSE': 'licenses/zstd/',
            'zstd/1.4.7/CHANGELOG': 'licenses/zstd/',
        }
    },

    ('libarchive', CURRENT_MACOSX_VERSION): {
        'fullversion': '3.4.3',
        'ignore_deps': [],
        'deletes': ['licenses', 'lib'],
        'install_dir': 'builtins/extractcode_libarchive-macosx/src/extractcode_libarchive',

        'thirdparty_dir': 'builtins/extractcode_libarchive-macosx/thirdparty',

        'base_dir': 'builtins/extractcode_libarchive-macosx',
        'source_plugins_dir': 'builtins/extractcode_libarchive-sources',

        'fixes': [
            ('patchmacho', 'lib/libarchive.dylib'),
            ('patchmacho', 'lib/libb2.1.dylib'),
            ('patchmacho', 'lib/liblz4.1.dylib'),
            ('patchmacho', 'lib/liblzma.5.dylib'),
            ('patchmacho', 'lib/libzstd.1.dylib'),
        ],

        'copies': {
            'libarchive/3.4.3/lib/libarchive.13.dylib': 'lib/libarchive.dylib',
            'libarchive/3.4.3/INSTALL_RECEIPT.json': 'licenses/libarchive/',
            'libarchive/3.4.3/COPYING': 'licenses/libarchive/',
            'libarchive/3.4.3/README.md': 'licenses/libarchive/',

            'libb2/0.98.1/lib/libb2.1.dylib': 'lib/',
            'libb2/0.98.1/INSTALL_RECEIPT.json': 'licenses/libb2/',
            'libb2/0.98.1/COPYING': 'licenses/libb2/',

            'lz4/1.9.3/lib/liblz4.1.dylib': 'lib/',
            'lz4/1.9.3/INSTALL_RECEIPT.json': 'licenses/lz4/',
            'lz4/1.9.3/LICENSE': 'licenses/lz4/',
            'lz4/1.9.3/README.md': 'licenses/lz4/',
            'lz4/1.9.3/include/lz4frame_static.h': 'licenses/lz4/lz4.LICENSE',

            'xz/5.2.5/lib/liblzma.5.dylib': 'lib/',
            'xz/5.2.5/INSTALL_RECEIPT.json': 'licenses/xz/',
            'xz/5.2.5/COPYING': 'licenses/xz/',
            'xz/5.2.5/README': 'licenses/xz/',
            'xz/5.2.5/AUTHORS': 'licenses/xz/',
            'xz/5.2.5/share/doc/xz/THANKS': 'licenses/xz/',
            'xz/5.2.5/ChangeLog': 'licenses/xz/',

            'zstd/1.4.7/lib/libzstd.1.dylib': 'lib/',
            'zstd/1.4.7/INSTALL_RECEIPT.json': 'licenses/zstd/',
            'zstd/1.4.7/COPYING': 'licenses/zstd/',
            'zstd/1.4.7/README.md': 'licenses/zstd/',
            'zstd/1.4.7/LICENSE': 'licenses/zstd/',
            'zstd/1.4.7/CHANGELOG': 'licenses/zstd/',
        }
    },

    ('p7zip', 'x86_64_linux'): {
        'fullversion': '16.02_2',
        'install_dir': 'builtins/extractcode_7z-linux/src/extractcode_7z',
        'thirdparty_dir': 'builtins/extractcode_7z-linux/thirdparty',

        'base_dir': 'builtins/extractcode_7z-linux',
        'source_plugins_dir': 'builtins/extractcode_7z-sources',

        'ignore_deps': [],
        'deletes': ['licenses', 'lib', 'bin', 'doc'],
        'fixes': [
            ('patchelf', '--set-rpath', '$ORIGIN/.', 'bin/7z.so'),
            ('patchelf', '--set-rpath', '$ORIGIN/.', 'bin/7z'),
            ('patchelf', '--set-interpreter', '/lib64/ld-linux-x86-64.so.2', 'bin/7z'),
        ],
        'copies': {
            'p7zip/16.02_2/lib/p7zip/7z': 'bin/',
            'p7zip/16.02_2/lib/p7zip/7z.so': 'bin/',

            'p7zip/16.02_2/INSTALL_RECEIPT.json': 'licenses/p7zip/',
            'p7zip/16.02_2/share/doc/p7zip/README': 'licenses/p7zip/',
            'p7zip/16.02_2/share/doc/p7zip/DOC/License.txt': 'licenses/p7zip/',
            'p7zip/16.02_2/share/doc/p7zip/DOC/copying.txt': 'licenses/p7zip/',
            'p7zip/16.02_2/share/doc/p7zip/DOC/unRarLicense.txt': 'licenses/p7zip/',
            'p7zip/16.02_2/share/doc/p7zip/DOC/readme.txt': 'licenses/p7zip/',
            'p7zip/16.02_2/share/doc/p7zip/DOC/src-history.txt': 'licenses/p7zip/',
            'p7zip/16.02_2/share/doc/p7zip/ChangeLog': 'licenses/p7zip/',
        },
    },

    ('p7zip', CURRENT_MACOSX_VERSION): {
        'fullversion': '16.02_2',
        'install_dir': 'builtins/extractcode_7z-macosx/src/extractcode_7z',
        'thirdparty_dir': 'builtins/extractcode_7z-macosx/thirdparty',

        'base_dir': 'builtins/extractcode_7z-macosx',
        'source_plugins_dir': 'builtins/extractcode_7z-sources',

        'ignore_deps': [],
        'deletes': ['licenses', 'lib', 'bin', 'doc'],
        'copies': {
            'p7zip/16.02_2/lib/p7zip/7z': 'bin/',
            'p7zip/16.02_2/lib/p7zip/7z.so': 'bin/',

            'p7zip/16.02_2/INSTALL_RECEIPT.json': 'licenses/p7zip/',
            'p7zip/16.02_2/share/doc/p7zip/README': 'licenses/p7zip/',
            'p7zip/16.02_2/share/doc/p7zip/DOC/License.txt': 'licenses/p7zip/',
            'p7zip/16.02_2/share/doc/p7zip/DOC/copying.txt': 'licenses/p7zip/',
            'p7zip/16.02_2/share/doc/p7zip/DOC/unRarLicense.txt': 'licenses/p7zip/',
            'p7zip/16.02_2/share/doc/p7zip/DOC/readme.txt': 'licenses/p7zip/',
            'p7zip/16.02_2/share/doc/p7zip/DOC/src-history.txt': 'licenses/p7zip/',
            'p7zip/16.02_2/share/doc/p7zip/ChangeLog': 'licenses/p7zip/',
        },
    },

    ('libmagic', 'x86_64_linux'): {
        'fullversion': '5.39',
        'install_dir': 'builtins/typecode_libmagic-linux/src/typecode_libmagic',
        'thirdparty_dir': 'builtins/typecode_libmagic-linux/thirdparty',

        'base_dir': 'builtins/typecode_libmagic-linux',
        'source_plugins_dir': 'builtins/typecode_libmagic-sources',

        'ignore_deps': [],
        'deletes': ['licenses', 'lib', 'bin', 'doc'],
        'fixes': [
            ('patchelf', '--set-rpath', '$ORIGIN/.', 'lib/libmagic.so'),
            ('patchelf', '--replace-needed', 'libz.so.1' , 'libz-lm539.so.1', 'lib/libmagic.so'),

            ('patchelf', '--set-rpath', '$ORIGIN/.', 'lib/libz-lm539.so.1'),
            ('patchelf', '--set-soname', 'libz-lm539.so.1', 'lib/libz-lm539.so.1'),
        ],
        'copies': {
            'libmagic/5.39/lib/libmagic.so': 'lib/',
            'libmagic/5.39/share/misc/magic.mgc': 'data/',
            'libmagic/5.39/INSTALL_RECEIPT.json': 'licenses/libmagic/',
            'libmagic/5.39/COPYING': 'licenses/libmagic/',
            'libmagic/5.39/README': 'licenses/libmagic/',
            'libmagic/5.39/AUTHORS': 'licenses/libmagic/',
            'libmagic/5.39/ChangeLog': 'licenses/libmagic/',

            'zlib/1.2.11/lib/libz.so.1': 'lib/libz-lm539.so.1',
            'zlib/1.2.11/INSTALL_RECEIPT.json': 'licenses/zlib/',
            'zlib/1.2.11/README': 'licenses/zlib/',
            'zlib/1.2.11/ChangeLog': 'licenses/zlib/',
        },
    },
    ('libmagic', CURRENT_MACOSX_VERSION): {
        'fullversion': '5.39',
        'install_dir': 'builtins/typecode_libmagic-macosx/src/typecode_libmagic',
        'thirdparty_dir': 'builtins/typecode_libmagic-macosx/thirdparty',

        'base_dir': 'builtins/typecode_libmagic-macosx',
        'source_plugins_dir': 'builtins/typecode_libmagic-sources',

        'ignore_deps': [],
        'deletes': ['licenses', 'lib', 'bin', 'doc'],
        'fixes': [
            ('patchmacho', 'lib/libmagic.dylib'),
        ],

        'copies': {
            'libmagic/5.39/lib/libmagic.1.dylib': 'lib/libmagic.dylib',
            'libmagic/5.39/share/misc/magic.mgc': 'data/',
            'libmagic/5.39/INSTALL_RECEIPT.json': 'licenses/libmagic/',
            'libmagic/5.39/COPYING': 'licenses/libmagic/',
            'libmagic/5.39/README': 'licenses/libmagic/',
            'libmagic/5.39/AUTHORS': 'licenses/libmagic/',
            'libmagic/5.39/ChangeLog': 'licenses/libmagic/',
        },
    },

}

if __name__ == '__main__':
    sys.exit(main(sys.argv))
