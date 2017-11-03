from django.views import generic
from django.urls import URLPattern, URLResolver, path, include
from django.urls.resolvers import RoutePattern

from material.utils import strip_suffixes, camel_case_to_underscore

DEFAULT = object()


class _viewset_items(dict):
    """Track the order of declared views and child viewsets."""

    def __init__(self):
        self.items = []

    def __setitem__(self, key, value):
        is_item = key not in self and (
            key.endswith('_url') or
            key.endswith('_viewset')
        )
        if is_item:
            self.items.append(key)
        dict.__setitem__(self, key, value)


class ViewsetMetaClass(type):
    """
    Metaclass that tracks order of viewset attributes.
    """

    @classmethod
    def __prepare__(metacls, name, bases):
        return _viewset_items()

    def __new__(cls, name, bases, classdict):
        result = type.__new__(cls, name, bases, dict(classdict))

        items = []

        metabases = (
            base for base in bases
            if hasattr(base, '_viewset_items')
        )
        for metabase in metabases:
            items += metabase._viewset_items

        for item in classdict.items:
            if item not in items:
                items.append(item)

        result._viewset_items = items

        return result


class viewprop(object):
    """
    A property that can be overridden.
    """
    def __init__(self, func):
        self.__doc__ = getattr(func, '__doc__')
        self.fget = func

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        if self.fget.__name__ in obj.__dict__:
            return obj.__dict__[self.fget.__name__]
        return self.fget(obj)

    def __set__(self, obj, value):
        obj.__dict__[self.fget.__name__] = value

    def __repr__(self):
        return '<view_property func={}>'.format(self.fget)


class Viewset(metaclass=ViewsetMetaClass):
    """
    Viewset is the urlpatterns list on steroids

    Viewset class automatically collect URL patterns from class attributes
    with names ends with `_url` and auto-create url patterns from  attributes
    with names ends with `_viewset`

    Viewset classes could be inherited, extended, and have overridden attributes.

    Example::

        class SiteViewset(Viewset):
            index_url = path('', IndexView.as_view(), name="index")

            # employees = EmployeesViewset(prefix='emp', app_name='employees')
            # path('employees', employees.urls)
            employees_viewset = EmployeesViewset(prefix='emp')

        urlpatters = [
            SiteViewset(app_name="site").urls,
        ]

        reverse('site:employees:index')

    .. seealso::
        `Django URL namespaces
        https://docs.djangoproject.com/en/1.11/topics/http/urls/#url-namespaces>`_
    """

    app_name = None
    namespace = None
    prefix = None

    def __init__(self, *, prefix=None, app_name=None, namespace=None, **initkwargs):
        """
        Instantiate a viewset instance.

        :param prefix: an URL prefix. Could be used by parent viewset.
        :param app_name: application URL namespace
        :param namespace: instance URL namespace
        """
        self._urls = None

        self.prefix = prefix
        self.app_name = app_name
        self.namespace = namespace

        for key, value in initkwargs.items():
            if not hasattr(self.__class__, key):
                raise TypeError(
                    "{}() received an invalid keyword {}. Viewset constructor "
                    "only accepts arguments that are already "
                    "attributes of the class." .format(self.__name__, key))
            setattr(self, key, value)

        super().__init__()

    def get_viewset_pattern(self, attr_name, viewset):
        prefix = viewset.prefix
        patterns, app_name, namespace = viewset.urls
        default = strip_suffixes(attr_name, ["_viewset"])

        if prefix is None:
            prefix = default

        if app_name is None:
            name = default

        if namespace is None:
            namespace = default

        return path('{}/'.format(prefix), include((patterns, name), namespace=namespace))

    def get_urls(self):
        """
        Collect URLs from the instance attributes.

        Assumes that each attribute with name ending on `_view`
        contains url pattern declaration.

        Attributes ends on `_viewset` is the Viewset class instance
        """
        urlpatterns = []

        for attr_name in self._viewset_items:
            attr = getattr(self, attr_name)
            if attr is None:
                continue

            if attr_name.endswith('_url') and isinstance(attr, URLPattern):
                urlpatterns.append(attr)
            elif attr_name.endswith('_viewset') and isinstance(attr, Viewset):
                urlpatterns.append(self.get_viewset_pattern(attr_name, attr))
            else:
                raise ValueError('Unknown {}.{} entry type'.format(self.__class__.__name__, attr_name))

        return urlpatterns

    @property
    def urls(self):
        namespace = self.namespace
        if namespace is None:
            namespace = self.app_name
        if self._urls is None:
            self._urls = self.get_urls()
        return self._urls, self.app_name, namespace


def _get_viewset_index_url(viewset):
    """
    Return first non-parameterized viewset url.
    """
    def _get_index_url(url_patterns, prefix='./'):
        for url_pattern in url_patterns:
            if isinstance(url_pattern, URLPattern):
                couldbe_index_view = (
                    isinstance(url_pattern.pattern, RoutePattern) and
                    url_pattern.pattern.converters == {} and
                    not (hasattr(url_pattern.callback, 'view_class') and
                         url_pattern.callback.view_class == IndexRedirectView)
                )
                if couldbe_index_view:
                    return prefix + url_pattern.pattern._route
            elif isinstance(url_pattern, URLResolver) and isinstance(url_pattern.pattern, RoutePattern):
                return _get_index_url(url_pattern.url_patterns, prefix+url_pattern.pattern._route)
    return _get_index_url(viewset.urls[0], './')


class IndexRedirectView(generic.RedirectView):
    viewset = None

    def get_redirect_url(self, *args, **kwargs):
        if self.viewset:
            redirect = _get_viewset_index_url(self.viewset)
            if redirect is None:
                raise ValueError(
                    "Can't determine index url. "
                    "Please remove IndexViewMixin and add an explicit"
                    "`index_url = path('', generics.RedirectView(url='...'), name='index')`"
                    " declaration for the viewset")
            return redirect
        return super().get_redirect_url(*args, **kwargs)


class IndexViewMixin(metaclass=ViewsetMetaClass):
    """
    Redirect from / to the first non-parameterized view of the viewset.
    """
    @property
    def index_url(self):
        return path('', IndexRedirectView.as_view(viewset=self), name="index")


class NamedViewsetMixin:
    """
    Auto-derive viewset namespace from the viewset class name.
    """
    name = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        viewset_name = camel_case_to_underscore(
            strip_suffixes(
                self.__class__.__name__,
                ['Application', 'Viewset', 'Admin']))

        if self.name is None:
            self.app_name = viewset_name