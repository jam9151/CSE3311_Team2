from django.contrib.staticfiles.apps import StaticFilesConfig


class MediaflowStaticFilesConfig(StaticFilesConfig):
    """The stock staticfiles app, except `collectstatic` skips static/src.

    That folder is the Tailwind source, which is built into css/app.css and
    isn't meant to be served. It also can't be collected: it imports a package
    that only exists at build time, and hashing the file names fails on it.
    """

    ignore_patterns = StaticFilesConfig.ignore_patterns + ["src"]
