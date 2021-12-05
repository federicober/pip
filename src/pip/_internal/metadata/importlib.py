import email.message
import importlib.metadata
import os
import pathlib
import sys
import zipfile
from typing import (
    Collection,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Protocol,
    Sequence,
    Set,
)

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import NormalizedName, canonicalize_name
from pip._vendor.packaging.version import parse as parse_version

from pip._internal.exceptions import InvalidWheel, UnsupportedWheel
from pip._internal.utils.packaging import safe_extra
from pip._internal.utils.wheel import parse_wheel, read_wheel_metadata_file

from .base import (
    BaseDistribution,
    BaseEntryPoint,
    BaseEnvironment,
    DistributionVersion,
    InfoPath,
    Wheel,
)


def _get_dist_normalized_name(dist: importlib.metadata.Distribution) -> NormalizedName:
    """Get the distribution's project name.

    The ``name`` attribute is only available in Python 3.10 or later. We are
    targeting exactly that, but Mypy does not know this.
    """
    return canonicalize_name(dist.name)  # type: ignore[attr-defined]


class BasePath(Protocol):
    """A protocol that various path objects conform.

    This exists because importlib.metadata uses both ``pathlib.Path`` and
    ``zipfile.Path``, and we need a common base for type hints (Union does not
    work well since ``zipfile.Path`` is too new for our linter setup).

    This does not mean to be exhaustive, but only contains things that present
    in both classes *that we need*.
    """

    name: str


class RequiresEntry(NamedTuple):
    requirement: str
    extra: str
    marker: str


class WheelDistribution(importlib.metadata.Distribution):
    """Distribution read from a wheel.

    Although ``importlib.metadata.PathDistribution`` accepts ``zipfile.Path``,
    its implementation is too "lazy" for pip's needs (we can't keep the ZipFile
    handle open for the entire lifetime of the distribution object).

    This implementation eagerly reads the entire metadata directory into the
    memory instead, and operates from that.
    """

    def __init__(
        self,
        files: Mapping[pathlib.PurePosixPath, bytes],
        info_location: pathlib.PurePosixPath,
    ) -> None:
        self._files = files
        self.info_location = info_location

    @classmethod
    def from_zipfile(
        cls,
        zf: zipfile.ZipFile,
        name: str,
        location: str,
    ) -> "WheelDistribution":
        info_dir, _ = parse_wheel(zf, name)
        paths = (
            (name, pathlib.PurePosixPath(name.split("/", 1)[-1]))
            for name in zf.namelist()
            if name.startswith(f"{info_dir}/")
        )
        files = {
            relpath: read_wheel_metadata_file(zf, fullpath)
            for fullpath, relpath in paths
        }
        info_location = pathlib.PurePosixPath(location, info_dir)
        return cls(files, info_location)

    def iterdir(self, path: InfoPath) -> Iterator[pathlib.PurePosixPath]:
        # Only allow iterating through the metadata directory.
        if pathlib.PurePosixPath(str(path)) in self._files:
            return iter(self._files)
        raise FileNotFoundError(path)

    def read_text(self, filename: str) -> Optional[str]:
        try:
            data = self._files[pathlib.PurePosixPath(filename)]
        except KeyError:
            return None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            wheel = self.info_location.parent
            error = f"Error decoding metadata for {wheel}: {e} in {filename} file"
            raise UnsupportedWheel(error)
        return text


class Distribution(BaseDistribution):
    def __init__(
        self,
        dist: importlib.metadata.Distribution,
        location: BasePath,
        info_location: Optional[BasePath],
    ) -> None:
        self._dist = dist
        self._location = location
        self._info_location = info_location

    @classmethod
    def from_directory(cls, directory: str) -> BaseDistribution:
        info_location = pathlib.Path(directory)
        dist = importlib.metadata.Distribution.at(info_location)
        location = info_location.parent
        return cls(dist, location, info_location)

    @classmethod
    def from_wheel(cls, wheel: Wheel, name: str) -> BaseDistribution:
        try:
            with wheel.as_zipfile() as zf:
                dist = WheelDistribution.from_zipfile(zf, name, wheel.location)
        except zipfile.BadZipFile as e:
            raise InvalidWheel(wheel.location, name) from e
        except UnsupportedWheel as e:
            raise UnsupportedWheel(f"{name} has an invalid wheel, {e}")
        return cls(dist, pathlib.PurePosixPath(wheel.location), dist.info_location)

    @property
    def location(self) -> Optional[str]:
        return str(self._location)

    @property
    def info_location(self) -> Optional[str]:
        if self._info_location is None:
            return None
        return str(self._info_location)

    @property
    def canonical_name(self) -> NormalizedName:
        # Try to get the name from the metadata directory name. This is much
        # faster than reading metadata.
        if self._info_location is None:
            return _get_dist_normalized_name(self._dist)
        stem, suffix = os.path.splitext(self._info_location.name)
        if suffix not in (".dist-info", ".egg-info"):
            return _get_dist_normalized_name(self._dist)
        name, _, _ = stem.partition("-")
        return canonicalize_name(name)

    @property
    def version(self) -> DistributionVersion:
        return parse_version(self._dist.version)

    def is_file(self, path: InfoPath) -> bool:
        return self._dist.read_text(str(path)) is not None

    def iter_distutils_script_names(self) -> Iterator[str]:
        if not isinstance(self._info_location, pathlib.Path):
            return
        for child in self._info_location.joinpath("scripts").iterdir():
            yield child.name

    def read_text(self, path: InfoPath) -> str:
        content = self._dist.read_text(str(path))
        if content is None:
            raise FileNotFoundError(path)
        return content

    def iter_entry_points(self) -> Iterable[BaseEntryPoint]:
        # importlib.metadata's EntryPoint structure sasitfies BaseEntryPoint.
        return self._dist.entry_points

    @property
    def metadata(self) -> email.message.Message:
        return self._dist.metadata

    def _iter_requires_txt_entries(self) -> Iterator[RequiresEntry]:
        """Parse a ``requires.txt`` in an egg-info directory.

        This is an INI-ish format where an egg-info stores dependencies. A
        section name describes extra other environment markers, while each entry
        is an arbitrary string (not a key-value pair) representing a dependency
        as a requirement string (no markers).

        There is a construct in ``importlib.metadata`` called ``Sectioned`` that
        does mostly the same, but the format is currently considered private.
        """
        content = self._dist.read_text("requires.txt")
        if content is None:
            return
        extra = marker = ""  # Section-less entries don't have markers.
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):  # Comment; ignored.
                continue
            if line.startswith("[") and line.endswith("]"):  # A section header.
                extra, _, marker = line.strip("[]").partition(":")
                continue
            yield RequiresEntry(requirement=line, extra=extra, marker=marker)

    def _iter_egg_info_extras(self) -> Iterable[str]:
        """Get extras from the egg-info directory."""
        known_extras = {""}
        for entry in self._iter_requires_txt_entries():
            if entry.extra in known_extras:
                continue
            known_extras.add(entry.extra)
            yield entry.extra

    def iter_provided_extras(self) -> Iterable[str]:
        iterator = (
            self._dist.metadata.get_all("Provides-Extra")
            or self._iter_egg_info_extras()
        )
        return (safe_extra(extra) for extra in iterator)

    def _iter_egg_info_dependencies(self) -> Iterable[str]:
        """Get distribution dependencies from the egg-info directory.

        To ease parsing, this converts a legacy dependency entry into a PEP 508
        requirement string. Like ``_iter_requires_txt_entries()``, there is code
        in ``importlib.metadata`` that does mostly the same, but not do exactly
        what we need.

        Namely, ``importlib.metadata`` does not normalize the extra name before
        putting it into the requirement string, which causes marker comparison
        to fail because the dist-info format do normalize. This is consistent in
        all currently available PEP 517 backends, although not standardized.
        """
        for entry in self._iter_requires_txt_entries():
            if entry.extra and entry.marker:
                marker = f'({entry.marker}) and extra == "{safe_extra(entry.extra)}"'
            elif entry.extra:
                marker = f'extra == "{safe_extra(entry.extra)}"'
            elif entry.marker:
                marker = entry.marker
            else:
                marker = ""
            if marker:
                yield f"{entry.requirement} ; {marker}"
            else:
                yield entry.requirement

    def iter_dependencies(self, extras: Collection[str] = ()) -> Iterable[Requirement]:
        req_string_iterator = (
            self._dist.metadata.get_all("Requires-Dist")
            or self._iter_egg_info_dependencies()
        )
        contexts: Sequence[Dict[str, str]] = [{"extra": safe_extra(e)} for e in extras]
        for req_string in req_string_iterator:
            req = Requirement(req_string)
            if not req.marker:
                yield req
            elif not extras and req.marker.evaluate({"extra": ""}):
                yield req
            elif any(req.marker.evaluate(context) for context in contexts):
                yield req


def _get_info_location(d: importlib.metadata.Distribution) -> Optional[BasePath]:
    """Find the path to the distribution's metadata directory.

    HACK: This relies on importlib.metadata's private ``_path`` attribute. Not
    all distributions exist on disk, so importlib.metadata is correct to not
    expose the attribute as public. But pip's code base is old and not as clean,
    so we do this to avoid having to rewrite too many things. Hopefully we can
    eliminate this some day.
    """
    return getattr(d, "_path", None)


class Environment(BaseEnvironment):
    def __init__(self, paths: Sequence[str]) -> None:
        self._paths = paths

    @classmethod
    def default(cls) -> BaseEnvironment:
        return cls(sys.path)

    @classmethod
    def from_paths(cls, paths: Optional[List[str]]) -> BaseEnvironment:
        if paths is None:
            return cls(sys.path)
        return cls(paths)

    def _iter_distributions(self) -> Iterator[BaseDistribution]:
        # To know exact where we found a distribution, we have to feed the paths
        # in one by one, instead of dumping entire list to importlib.metadata.
        found_names: Set[NormalizedName] = set()
        for path in self._paths:
            for dist in importlib.metadata.distributions(path=[path]):
                normalized_name = _get_dist_normalized_name(dist)
                if normalized_name in found_names:
                    continue
                found_names.add(normalized_name)
                location = pathlib.Path(path)
                info_location = _get_info_location(dist)
                yield Distribution(dist, location, info_location)

    def get_distribution(self, name: str) -> Optional[BaseDistribution]:
        matches = (
            distribution
            for distribution in self.iter_all_distributions()
            if distribution.canonical_name == canonicalize_name(name)
        )
        return next(matches, None)
