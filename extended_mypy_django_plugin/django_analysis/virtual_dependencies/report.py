from __future__ import annotations

import collections
import dataclasses
import functools
import os
import pathlib
import shutil
from collections.abc import Callable, Iterator, MutableMapping, MutableSet, Sequence
from typing import TYPE_CHECKING, Generic, TypeVar, cast

from .. import protocols
from ..discovery import ImportPath
from . import dependency

T_Report = TypeVar("T_Report", bound="Report")


@dataclasses.dataclass(frozen=True, kw_only=True)
class Report:
    concrete_annotations: MutableMapping[protocols.ImportPath, protocols.ImportPath] = (
        dataclasses.field(default_factory=dict)
    )
    concrete_querysets: MutableMapping[protocols.ImportPath, protocols.ImportPath] = (
        dataclasses.field(default_factory=dict)
    )
    report_import_path: MutableMapping[protocols.ImportPath, protocols.ImportPath] = (
        dataclasses.field(default_factory=dict)
    )
    related_import_paths: MutableMapping[
        protocols.ImportPath, MutableSet[protocols.ImportPath]
    ] = dataclasses.field(default_factory=lambda: collections.defaultdict(set))

    def register_module(
        self,
        *,
        module_import_path: protocols.ImportPath,
        virtual_import_path: protocols.ImportPath,
    ) -> None:
        self.report_import_path[module_import_path] = virtual_import_path

    def register_model(
        self,
        *,
        model_import_path: protocols.ImportPath,
        virtual_import_path: protocols.ImportPath,
        concrete_name: str,
        concrete_queryset_name: str,
        concrete_models: Sequence[protocols.Model],
    ) -> None:
        module_import_path, name = ImportPath.split(model_import_path)

        self.concrete_annotations[model_import_path] = ImportPath(
            f"{virtual_import_path}.{concrete_name}"
        )
        self.concrete_querysets[model_import_path] = ImportPath(
            f"{virtual_import_path}.{concrete_queryset_name}"
        )

        for concrete in concrete_models:
            ns, _ = ImportPath.split(concrete.import_path)
            if ns != module_import_path:
                self.related_import_paths[module_import_path].add(ns)
                self.related_import_paths[ns].add(module_import_path)

            if concrete.default_custom_queryset:
                ns, _ = ImportPath.split(concrete.default_custom_queryset)
                if ns != module_import_path:
                    self.related_import_paths[module_import_path].add(ns)
                    self.related_import_paths[ns].add(module_import_path)

            for field in concrete.all_fields.values():
                if field.related_model:
                    ns, _ = ImportPath.split(field.related_model)
                    if ns != module_import_path:
                        self.related_import_paths[module_import_path].add(ns)
                        self.related_import_paths[ns].add(module_import_path)

            for mro in concrete.models_in_mro:
                ns, _ = ImportPath.split(mro)
                if ns != module_import_path:
                    self.related_import_paths[module_import_path].add(ns)
                    self.related_import_paths[ns].add(module_import_path)


@dataclasses.dataclass(frozen=True, kw_only=True)
class WrittenVirtualDependency(Generic[protocols.T_Report]):
    content: str
    summary_hash: str | None
    report: protocols.T_Report
    virtual_import_path: protocols.ImportPath


@dataclasses.dataclass(frozen=True, kw_only=True)
class VirtualDependencyScribe(Generic[protocols.T_VirtualDependency, protocols.T_Report]):
    hasher: protocols.Hasher
    report_maker: protocols.ReportMaker[protocols.T_Report]
    virtual_dependency: protocols.T_VirtualDependency
    all_virtual_dependencies: protocols.VirtualDependencyMap[protocols.T_VirtualDependency]

    def write(self) -> WrittenVirtualDependency[protocols.T_Report]:
        report = self.report_maker()
        virtual_import_path = self.virtual_dependency.summary.virtual_dependency_name
        return WrittenVirtualDependency(
            content="", summary_hash="", report=report, virtual_import_path=virtual_import_path
        )

    @classmethod
    def get_report_summary(cls, location: pathlib.Path) -> str | None:
        """
        Given some location return the summary from that location.

        If the report doesn't have a summary or is for a module that doesn't exist anymore then return None
        """
        return None


@dataclasses.dataclass(frozen=True, kw_only=True)
class ReportCombiner(Generic[T_Report]):
    reports: Sequence[T_Report]
    report_maker: protocols.ReportMaker[T_Report]

    def combine(self) -> T_Report:
        final = self.report_maker()
        for report in self.reports:
            final.concrete_annotations.update(report.concrete_annotations)
            final.concrete_querysets.update(report.concrete_querysets)
            final.report_import_path.update(report.report_import_path)

            for path, related in report.related_import_paths.items():
                final.related_import_paths[path] |= related

        return final


@dataclasses.dataclass(frozen=True, kw_only=True)
class ReportInstaller:
    _written: dict[pathlib.Path, str | None] = dataclasses.field(init=False, default_factory=dict)
    _get_report_summary: Callable[[pathlib.Path], str | None]

    def write_report(
        self,
        *,
        scratch_root: pathlib.Path,
        summary_hash: str | None,
        virtual_import_path: protocols.ImportPath,
        content: str,
    ) -> None:
        location = scratch_root / f"{virtual_import_path.replace('.', os.sep)}.py"
        if not location.is_relative_to(scratch_root):
            raise RuntimeError(
                f"Virtual dependency ends up being outside of the scratch root: {virtual_import_path}"
            )
        location.parent.mkdir(parents=True, exist_ok=True)
        location.write_text(content)
        self._written[location] = summary_hash

    def install_reports(self, *, scratch_root: pathlib.Path, destination: pathlib.Path) -> None:
        seen: set[pathlib.Path] = set()
        for location, summary in self._written.items():
            relative_path = location.relative_to(scratch_root)
            destination_path = destination / relative_path

            seen.add(destination_path)
            found_summary: str | None = None
            if destination_path.exists():
                found_summary = self._get_report_summary(destination_path)

            if found_summary != summary:
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(location, destination_path)

        for root, dirs, files in os.walk(destination):
            for name in list(dirs):
                location = pathlib.Path(root) / name
                if location not in seen:
                    if self._get_report_summary(location) is None:
                        shutil.rmtree(location)
                        dirs.remove(name)

            for name in files:
                location = pathlib.Path(root) / name
                if location not in seen:
                    if self._get_report_summary(location) is None:
                        location.unlink()


@dataclasses.dataclass(frozen=True, kw_only=True)
class ReportFactory(Generic[protocols.T_VirtualDependency, protocols.T_Report]):
    report_installer: protocols.ReportInstaller
    report_combiner_maker: protocols.ReportCombinerMaker[protocols.T_Report]
    report_maker: protocols.ReportMaker[protocols.T_Report]
    report_scribe: protocols.VirtualDependencyScribe[
        protocols.T_VirtualDependency, protocols.T_Report
    ]

    def deploy_scribes(
        self, virtual_dependencies: protocols.VirtualDependencyMap[protocols.T_VirtualDependency]
    ) -> Iterator[protocols.WrittenVirtualDependency[protocols.T_Report]]:
        for virtual_dependency in virtual_dependencies.values():
            yield self.report_scribe(
                virtual_dependency=virtual_dependency,
                all_virtual_dependencies=virtual_dependencies,
            )


def make_report_factory(
    *, hasher: protocols.Hasher
) -> protocols.ReportFactory[protocols.T_VirtualDependency, Report]:
    def report_scribe(
        *,
        virtual_dependency: protocols.T_VirtualDependency,
        all_virtual_dependencies: protocols.VirtualDependencyMap[protocols.T_VirtualDependency],
    ) -> protocols.WrittenVirtualDependency[Report]:
        return VirtualDependencyScribe(
            hasher=hasher,
            report_maker=Report,
            virtual_dependency=virtual_dependency,
            all_virtual_dependencies=all_virtual_dependencies,
        ).write()

    return ReportFactory(
        report_maker=Report,
        report_scribe=report_scribe,
        report_installer=ReportInstaller(
            _get_report_summary=VirtualDependencyScribe.get_report_summary
        ),
        report_combiner_maker=functools.partial(ReportCombiner, report_maker=Report),
    )


if TYPE_CHECKING:
    C_Report = Report
    C_ReportFactory = ReportFactory[dependency.C_VirtualDependency, C_Report]
    C_ReportCombiner = ReportCombiner[C_Report]
    C_ReportInstaller = ReportInstaller
    C_WrittenVirtualDependency = WrittenVirtualDependency[C_Report]

    _R: protocols.P_Report = cast(Report, None)
    _RF: protocols.P_ReportFactory = cast(
        ReportFactory[protocols.P_VirtualDependency, protocols.P_Report], None
    )
    _WVD: protocols.P_WrittenVirtualDependency = cast(
        WrittenVirtualDependency[protocols.P_Report], None
    )
    _RI: protocols.P_ReportInstaller = cast(ReportInstaller, None)

    _CRC: protocols.ReportCombiner[C_Report] = cast(C_ReportCombiner, None)
    _CRF: protocols.ReportFactory[dependency.C_VirtualDependency, C_Report] = cast(
        C_ReportFactory, None
    )
    _CRM: protocols.ReportMaker[C_Report] = C_Report
    _CWVD: protocols.WrittenVirtualDependency[C_Report] = cast(C_WrittenVirtualDependency, None)
