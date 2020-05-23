import argparse
import logging
import importlib
import inspect
import os
import re
import six
import yaml

from collections import defaultdict
from contextlib import contextmanager

import IPython
from pygments.console import ansiformat
from traitlets.config.loader import Config

from insights.parsr.query import *  # noqa
from insights.parsr.query import eq, matches, make_child_query as q  # noqa
from insights.parsr.query.boolean import FALSE, TRUE

from insights import apply_configs, create_context, datasource, dr, extract, load_default_plugins, load_packages, parse_plugins
from insights.core import plugins
from insights.core.context import HostContext
from insights.core.spec_factory import ContentProvider, RegistryPoint
from insights.formats import render
from insights.formats.text import render_links

Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

RULE_COLORS = {"fail": "red", "pass": "blue", "info": "magenta", "skip": "yellow"}


@contextmanager
def __create_new_broker(path):
    """
    Create a broker and populate it by evaluating the path with all
    registered datasources.

    Args:
        path (str): path to the archive or directory to analyze. ``None`` will
            analyze the current system.
    """
    datasources = dr.get_components_of_type(datasource)

    def make_broker(ctx):
        broker = dr.Broker()
        broker[ctx.__class__] = ctx

        dr.run(datasources, broker=broker)

        del broker[ctx.__class__]
        return broker

    if path:
        if os.path.isdir(path):
            ctx = create_context(path)
            yield (path, make_broker(ctx))
        else:
            with extract(path) as e:
                ctx = create_context(e.tmp_dir)
                yield (e.tmp_dir, make_broker(ctx))
    else:
        yield (os.curdir, make_broker(HostContext()))


def __get_available_models(broker):
    """
    Given a broker populated with datasources, return everything that could
    run based on them.
    """
    state = set(broker.instances.keys())
    models = {}

    for comp in dr.run_order(dr.COMPONENTS[dr.GROUPS.single]):
        if comp in dr.DELEGATES and not plugins.is_datasource(comp):
            if dr.DELEGATES[comp].get_missing_dependencies(state):
                continue

            if plugins.is_type(comp, (plugins.rule, plugins.condition, plugins.incident)):
                name = "_".join([dr.get_base_module_name(comp), dr.get_simple_name(comp)])
            else:
                name = dr.get_simple_name(comp)

            if name in models:
                prev = models[name]
                models[dr.get_name(prev).replace(".", "_")] = prev

                del models[name]
                name = dr.get_name(comp).replace(".", "_")

            models[name] = comp
            state.add(comp)

    return models


class __Models(dict):
    """
    Represents all components that may be available given the data being
    analyzed. Use models.find() to see them. Tab complete attributes to access
    them. Use help(models) for more info.

    Examples:

        >>> models.find("rpm")
        InstalledRpms (insights.parsers.installed_rpms.InstalledRpms)

        >>> rpms = models.InstalledRpms
        >>> rpms.newest("bash")
        0:bash-4.1.2-48.el6

        >>> models.find("(?i)yum")  # Prefix "(?i)" ignores case.
        YumConf (insights.parsers.yum_conf.YumConf, parser)
        YumLog (insights.parsers.yumlog.YumLog, parser)
        YumRepoList (insights.parsers.yum.YumRepoList, parser)
        └╌╌Incorrect line: 'repolist: 33296'
        YumReposD (insights.parsers.yum_repos_d.YumReposD, parser)

        >>> models.show_trees("rpm")
        insights.parsers.installed_rpms.InstalledRpms (parser)
        ┊   insights.specs.Specs.installed_rpms (unfiltered / lines)
        ┊   ┊╌╌╌╌╌TextFileProvider("'/home/csams/Downloads/archives/sosreport-example-20191225000000/sos_commands/rpm/package-data'")
        ┊   ┊   insights.specs.insights_archive.InsightsArchiveSpecs.installed_rpms (unfiltered / lines)
        ┊   ┊   ┊   insights.specs.insights_archive.InsightsArchiveSpecs.all_installed_rpms (unfiltered / lines)
        ┊   ┊   ┊   ┊   insights.core.context.HostArchiveContext ()
        ┊   ┊   insights.specs.sos_archive.SosSpecs.installed_rpms (unfiltered / lines)
        ┊   ┊   ┊╌╌╌╌╌TextFileProvider("'/home/csams/Downloads/archives/sosreport-example-20191225000000/sos_commands/rpm/package-data'")
        ┊   ┊   ┊   insights.core.context.SosArchiveContext ()
        ┊   ┊   insights.specs.default.DefaultSpecs.installed_rpms (unfiltered / lines)
        ┊   ┊   ┊   insights.specs.default.DefaultSpecs.docker_installed_rpms (unfiltered / lines)
        ┊   ┊   ┊   ┊   insights.core.context.DockerImageContext ()
        ┊   ┊   ┊   insights.specs.default.DefaultSpecs.host_installed_rpms (unfiltered / lines)
        ┊   ┊   ┊   ┊   insights.core.context.HostContext ()
    """
    def __init__(self, broker, models, cwd, cov):
        self._requested = set()
        self._broker = broker
        self._cwd = cwd
        self._cov = cov
        super().__init__(models)

    def __dir__(self):
        """ Enabled ipython autocomplete. """
        return sorted(self.keys())

    def _ipython_key_completions_(self):
        """ For autocomplete of keys when accessing models as a dict. """
        return sorted(self.keys())

    def __str__(self):
        return "{} components possibly available".format(len(self))

    def _get_color(self, comp):
        if comp in self._broker:
            if plugins.is_type(comp, plugins.rule) and self._broker[comp].get("type") == "skip":
                return "yellow"
            return "green"
        elif comp in self._broker.exceptions:
            return "red"
        elif comp in self._broker.missing_requirements:
            return "yellow"
        else:
            return ""

    def _dump_diagnostics(self, comp):
        print("Dependency Tree")
        print("===============")
        self._show_tree(comp)
        print()
        print("Missing Dependencies")
        print("====================")
        self._show_missing(comp)
        print()
        print("Exceptions")
        print("==========")
        self._show_exceptions(comp)

    @contextmanager
    def _coverage_tracking(self):
        if self._cov:
            self._cov.start()
            yield
            self._cov.stop()
        else:
            yield

    def evaluate_all(self, match=None, ignore=None):
        """
        Evaluate all components that match.

        Args:
            match (str, optional): regular expression for matching against
                the fully qualified name of components to keep.
            ignore (str, optional): regular expression for searching against
                the fully qualified name of components to ignore.
        """
        match, ignore = self._desugar_match_ignore(match, ignore)

        tasks = []
        for c in self.values():
            name = dr.get_name(c)
            if match.test(name) and not ignore.test(name):
                if not any([c in self._broker.instances,
                            c in self._broker.exceptions,
                            c in self._broker.missing_requirements]):
                    tasks.append(c)

        if not tasks:
            return

        with self._coverage_tracking():
            dr.run(tasks, broker=self._broker)
        self.find(match, ignore)

    def evaluate(self, name):
        """
        Evaluate a component and return its result. Prints diagnostic
        information in the case of failure. This function is useful when a
        component's name contains characters that aren't valid for python
        identifiers so you can't access it with models.<name>.

        Args:
            name (str): the name of the component as shown by ``.find()``.
        """
        comp = self.get(name) or dr.get_component(name)
        if not comp:
            return

        if not plugins.is_rule(comp):
            self._requested.add((name, comp))

        if comp in self._broker:
            return self._broker[comp]

        if comp in self._broker.exceptions or comp in self._broker.missing_requirements:
            self._dump_diagnostics(comp)
            return

        with self._coverage_tracking():
            val = dr.run(comp, broker=self._broker).get(comp)

        if comp not in self._broker:
            if comp in self._broker.exceptions or comp in self._broker.missing_requirements:
                self._dump_diagnostics(comp)
            else:
                print("{} chose to skip.".format(dr.get_name(comp)))
        return val

    def __getattr__(self, name):
        return self.evaluate(name)

    # TODO: lot of room for improvement here...
    def make_rule(self, path=None, overwrite=False, pick=None):
        """
        Attempt to generate a rule based on models used so far.

        Args:
            path(str): path to store the rule.
            overwrite (bool): whether to overwrite an existing file.
            pick (str): Optionally specify which lines or line ranges
                to use for the rule body. "1 2 3" gets lines 1,2 and 3.
                "1 3-5 7" gets line 1, lines 3 through 5, and line 7.
        """
        import IPython
        ip = IPython.get_ipython()
        ignore = [
            r"=.*models\.",
            r"^(%|!|help)",
            r"make_rule",
            r"models\.(show|find).*",
            r".*\?$",
            r"^(clear|pwd|cd *.*|ll|ls)$"
        ]

        if pick:
            lines = [r[2] for r in ip.history_manager.get_range_by_str(pick)]
        else:
            lines = []
            for r in ip.history_manager.get_range():
                l = r[2]
                if any(re.search(i, l) for i in ignore):
                    continue
                elif l.startswith("models."):
                    l = l[7:]
                lines.append(l)

        # the user asked for these models during the session.
        requested = sorted(self._requested, key=lambda i: i[0])

        # figure out what we need to import for filtering.
        filterable = defaultdict(list)
        for _, c in requested:
            for d in dr.get_dependency_graph(c):
                try:
                    if isinstance(d, RegistryPoint) and d.filterable:
                        n = d.__qualname__
                        cls = n.split(".")[0]
                        filterable[dr.get_module_name(d)].append((cls, n))
                except:
                    pass

        model_names = [dr.get_simple_name(i[1]) for i in requested]
        var_names = [i[0].lower() for i in requested]

        imports = [
            "from insights import rule, make_fail, make_info, make_pass  # noqa",
            "from insights.parsr.query import *  # noqa",
            ""
        ]

        for _, c in requested:
            mod = dr.get_module_name(c)
            name = dr.get_simple_name(c)
            imports.append("from {} import {}".format(mod, name))

        seen = set()
        if filterable:
            imports.append("")

        for k in sorted(filterable):
            for (cls, n) in filterable[k]:
                if (k, cls) not in seen:
                    imports.append("from {} import {}".format(k, cls))
                    seen.add((k, cls))

        seen = set()
        filters = []
        for k in sorted(filterable):
            for i in filterable[k]:
                if i not in seen:
                    filters.append("add_filter({}, ...)".format(i[1]))
                    seen.add(i)

        if filters:
            imports.append("from insights.core.filters import add_filter")

        filter_stanza = "\n".join(filters)
        import_stanza = "\n".join(imports)
        decorator = "@rule({})".format(", ".join(model_names))
        func_decl = "def report({}):".format(", ".join(var_names))
        body = "\n".join(["    " + x for x in lines]) if lines else "    pass"

        res = import_stanza
        if filter_stanza:
            res += "\n\n" + filter_stanza

        res += "\n\n\n" + decorator + "\n" + func_decl + "\n" + body

        if path:
            if not path.startswith("/"):
                realpath = os.path.realpath(path)
                if not realpath.startswith(self._cwd):
                    path = os.path.join(self._cwd, path)

            if os.path.exists(path) and not overwrite:
                print("{} already exists. Use overwrite=True to overwrite.".format(path))
                return

            if not os.path.exists(path) or overwrite:
                with open(path, "w") as f:
                    f.write(res)
                ip.magic("edit -x {}".format(path))
                print("Saved to {}".format(path))
        else:
            IPython.core.page.page(ip.pycolorize(res))

    def _desugar_match_ignore(self, match, ignore):
        if match is None:
            match = TRUE
        elif isinstance(match, str):
            if match in self:
                match = eq(dr.get_name(self[match]))
            else:
                match = matches(match)

        if ignore is None:
            ignore = FALSE
        elif isinstance(ignore, str):
            ignore = matches(ignore)

        return (match, ignore)

    def _show_missing(self, comp):
        try:
            req, alo = self._broker.missing_requirements[comp]
            name = dr.get_name(comp)
            print(name)
            print("-" * len(name))
            if req:
                print("Requires:")
                for r in req:
                    print("    {}".format(dr.get_name(r)))
            if alo:
                if req:
                    print()
                print("Requires At Least One From Each List:")
                for r in alo:
                    print("[")
                    for i in r:
                        print("    {}".format(dr.get_name(i)))
                    print("]")
        except:
            pass

    def _show_datasource(self, d, v, indent=""):
        try:
            filtered = "filtered" if dr.DELEGATES[d].filterable else "unfiltered"
            mode = "bytes" if dr.DELEGATES[d].raw else "lines"
        except:
            filtered = "unknown"
            mode = "unknown"

        desc = "{n} ({f} / {m})".format(n=dr.get_name(d), f=filtered, m=mode)
        color = self._get_color(d)

        print(indent + ansiformat(color, desc))

        if not v:
            return
        if not isinstance(v, list):
            v = [v]

        for i in v:
            if isinstance(i, ContentProvider):
                s = ansiformat(color, str(i))
            else:
                s = ansiformat(color, "<intermediate value>")
            print("{}\u250A\u254C\u254C\u254C\u254C\u254C{}".format(indent, s))

    def show_requested(self):
        """ Show the components you've worked with so far. """
        for name, comp in sorted(self._requested):
            print(ansiformat(self._get_color(comp), "{} {}".format(name, dr.get_name(comp))))

    def reset_requested(self):
        """ Reset requested state so you can work on a new rule. """
        ip = IPython.get_ipython()
        ip.history_manager.reset()
        self._requested.clear()

    def show_source(self, comp):
        """
        Show source for the given module, class, or function. Also accepts
        the string names, with the side effect that the component will be
        imported.
        """
        try:
            if isinstance(comp, six.string_types):
                comp = self.get(comp) or dr.get_component(comp) or importlib.import_module(comp)
            if comp in dr.DELEGATES:
                comp = inspect.getmodule(comp)
            ip = IPython.get_ipython()
            if self._cov:
                path, runnable, excluded, not_run, _ = self._cov.analysis2(comp)
                runnable, not_run = set(runnable), set(not_run)
                src = ip.pycolorize(inspect.getsource(comp)).splitlines()
                width = len(str(len(src)))
                template = "{0:>%s}" % width
                results = []
                for i, line in enumerate(src):
                    i = i + 1
                    prefix = template.format(i)
                    if i in runnable and i not in not_run:
                        color = "*green*"
                    else:
                        color = "gray"
                    results.append("{} {}".format(ansiformat(color, prefix), line))
                IPython.core.page.page("\n".join(results))
            else:
                ip.inspector.pinfo(comp, detail_level=1)
        except:
            pass

    def _get_type_name(self, comp):
        try:
            return dr.get_component_type(comp).__name__
        except:
            return ""

    def _get_rule_value_kind(self, val):
        if val is None:
            kind = None
        elif isinstance(val, plugins.make_response):
            kind = "fail"
        elif isinstance(val, plugins.make_pass):
            kind = "pass"
        elif isinstance(val, plugins.make_info):
            kind = "info"
        elif isinstance(val, plugins._make_skip):
            kind = "skip"
        else:
            kind = None
        return kind

    def _get_value(self, comp):
        try:
            val = self._broker[comp]
            if plugins.is_rule(comp):
                _type = val.__class__.__name__
                kind = self._get_rule_value_kind(val)
                color = RULE_COLORS.get(kind, "")
                return ansiformat(color, " [{}]".format(_type))
        except:
            pass
        return ""

    def _show_tree(self, node, indent="", depth=None):
        if depth is not None and depth == 0:
            return

        color = self._get_color(node)
        if plugins.is_datasource(node):
            self._show_datasource(node, self._broker.get(node), indent=indent)
        else:
            _type = self._get_type_name(node)
            name = dr.get_name(node)
            suffix = self._get_value(node)
            desc = ansiformat(color, "{n} ({t}".format(n=name, t=_type))
            print(indent + desc + suffix + ansiformat(color, ")"))

        dashes = "\u250A\u254C\u254C\u254C\u254C\u254C"
        if node in self._broker.exceptions:
            for ex in self._broker.exceptions[node]:
                print(indent + dashes + ansiformat(color, str(ex)))

        deps = dr.get_dependencies(node)
        next_indent = indent + "\u250A   "
        for d in deps:
            self._show_tree(d, next_indent, depth=depth if depth is None else depth - 1)

    def show_trees(self, match=None, ignore="spec", depth=None):
        """
        Show dependency trees of any components whether they're available or not.

        Args:
            match (str, optional): regular expression for matching against
                the fully qualified name of components to keep.
            ignore (str, optional): regular expression for searching against
                the fully qualified name of components to ignore.
        """
        match, ignore = self._desugar_match_ignore(match, ignore)

        graph = defaultdict(list)
        for c in dr.DELEGATES:
            name = dr.get_name(c)
            if match.test(name) and not ignore.test(name):
                graph[name].append(c)

        for name in sorted(graph):
            for c in graph[name]:
                self._show_tree(c, depth=depth)
                print()

    def show_failed(self, match=None, ignore="spec"):
        """
        Show names of any components that failed during evaluation. Ignores
        "spec" by default.

        Args:
            match (str, optional): regular expression for matching against
                the fully qualified name of components to keep.
            ignore (str, optional): regular expression for searching against
                the fully qualified name of components to ignore.
        """
        match, ignore = self._desugar_match_ignore(match, ignore)

        mid_dashes = "\u250A\u254C\u254C"
        bottom_dashes = "\u2514\u254C\u254C"
        for comp in sorted(self._broker.exceptions, key=dr.get_name):
            name = dr.get_name(comp)
            if match.test(name) and not ignore.test(name):
                color = self._get_color(comp)
                print(ansiformat(color, name))
                exes = self._broker.exceptions[comp]
                last = len(exes) - 1
                for i, ex in enumerate(exes):
                    dashes = bottom_dashes if i == last else mid_dashes
                    print(ansiformat(color, dashes + str(ex)))
                print()

    def _show_exceptions(self, comp):
        name = dr.get_name(comp)
        print(name)
        print("-" * len(name))
        for e in self._broker.exceptions.get(comp, []):
            t = self._broker.tracebacks.get(e)
            if t:
                print(t)

    def show_exceptions(self, match=None, ignore="spec"):
        """
        Show exceptions that occurred during evaluation. Ignores "spec" by
        default.

        Args:
            match (str, optional): regular expression for matching against
                the fully qualified name of components to keep.
            ignore (str, optional): regular expression for searching against
                the fully qualified name of components to ignore.
        """
        match, ignore = self._desugar_match_ignore(match, ignore)

        for comp in sorted(self._broker.exceptions, key=dr.get_name):
            name = dr.get_name(comp)
            if match.test(name) and not ignore.test(name):
                self._show_exceptions(comp)

    def show_rule_report(self, match=None, ignore=None):
        """
        Print a rule report for the matching rules.
        """
        match, ignore = self._desugar_match_ignore(match, ignore)
        results = defaultdict(dict)

        for comp, val in self._broker.items():
            name = dr.get_name(comp)
            if plugins.is_rule(comp) and match.test(name) and not ignore.test(name):
                kind = self._get_rule_value_kind(val)

                if kind:
                    links = render_links(comp)
                    body = render(comp, val)
                    results[kind][name] = "\n".join([links, body])

        report = []
        for kind in ["info", "pass", "fail"]:
            color = RULE_COLORS.get(kind, "")
            hits = results.get(kind, {})
            for name in sorted(hits):
                report.append(ansiformat(color, name))
                report.append(ansiformat(color, "-" * len(name)))
                report.append(hits[name])
                report.append("")
        IPython.core.page.page("\n".join(report))

    def find(self, match=None, ignore=None):
        """
        Find components that might be available based on the data being
        analyzed.

        Args:
            match (str, optional): regular expression for matching against
                the fully qualified name of components to keep.
            ignore (str, optional): regular expression for searching against
                the fully qualified name of components to ignore.
        """
        match, ignore = self._desugar_match_ignore(match, ignore)
        mid_dashes = "\u250A\u254C\u254C"
        bottom_dashes = "\u2514\u254C\u254C"
        for p in sorted(self, key=str.lower):
            comp = self[p]
            name = dr.get_name(comp)
            if match.test(name) and not ignore.test(name):
                color = self._get_color(comp)
                _type = self._get_type_name(comp)
                suffix = self._get_value(comp)
                desc = ansiformat(color, "{p} ({n}, {t}".format(p=p, n=name, t=_type))
                print(desc + suffix + ansiformat(color, ")"))
                if comp in self._broker.exceptions:
                    exes = self._broker.exceptions[comp]
                    last = len(exes) - 1
                    for i, ex in enumerate(exes):
                        dashes = bottom_dashes if i == last else mid_dashes
                        print(ansiformat(color, dashes + str(ex)))


def start_session(__path, change_directory=False, __coverage=False):
    with __create_new_broker(__path) as (__working_path, __broker):
        __cwd = os.path.abspath(os.curdir)
        __models = __get_available_models(__broker)
        if __coverage:
            from coverage import Coverage
            __cov = Coverage(check_preimported=True, cover_pylib=False)
        else:
            __cov = None

        models = __Models(__broker, __models, __cwd, __cov)
        if change_directory:
            os.chdir(__working_path)

        # disable jedi since it won't autocomplete for objects with__getattr__
        # defined.
        IPython.core.completer.Completer.use_jedi = False
        __cfg = Config()
        __cfg.TerminalInteractiveShell.banner1 = __Models.__doc__
        __ns = {}
        __ns.update(globals())
        __ns.update(locals())
        IPython.start_ipython([], user_ns=__ns, config=__cfg)

        if __cov:
            __cov.erase()
        # TODO: we could automatically save the session here
        # see Models.make_rule
        if change_directory:
            os.chdir(__cwd)


def __handle_config(config):
    if config:
        with open(config) as f:
            apply_configs(yaml.load(f, Loader=Loader))


def __parse_args():
    desc = "Perform interactive system analysis with insights components."
    epilog = """
        Set env INSIGHTS_FILTERS_ENABLED=False to disable filtering that may
        cause unexpected missing data.
    """.strip()
    p = argparse.ArgumentParser(description=desc, epilog=epilog)

    p.add_argument("-p", "--plugins", default="", help="Comma separated list of packages to load.")
    p.add_argument("-c", "--config", help="The insights configuration to apply.")
    p.add_argument("--no-coverage", action="store_true", help="Don't show code coverage when viewing source.")
    p.add_argument("--cd", action="store_true", help="Change into the expanded directory for analysis.")
    p.add_argument("--no-defaults", action="store_true", help="Don't load default components.")
    p.add_argument("-v", "--verbose", action="store_true", help="Global debug level logging.")

    path_desc = "Archive or path to analyze. Leave off to target the current system."
    p.add_argument("path", nargs="?", help=path_desc)

    return p.parse_args()


def main():
    args = __parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.ERROR)

    if not args.no_defaults:
        load_default_plugins()
        dr.load_components("insights.parsers", "insights.combiners")

    load_packages(parse_plugins(args.plugins))
    __handle_config(args.config)

    start_session(args.path, args.cd, __coverage=not args.no_coverage)


if __name__ == "__main__":
    main()
