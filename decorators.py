from django.utils.translation import ugettext_lazy as _
from functools import wraps
from django.utils import six

# Todo Need to be implement Permission deny response without raise exception


class PermissionDenied():
    default_message = _('You do not have permission to perform this action')

    def __new__(self, message=None):
        if message is None:
            message = self.default_message
        return (self.default_message)


def context(f):
    def decorator(func):
        def wrapper(*args, **kwargs):
            info = args[f.__code__.co_varnames.index('info')]
            return func(info.context, *args, **kwargs)
        return wrapper
    return decorator


def user_passes_test(test_func):
    def decorator(f):
        @wraps(f)
        @context(f)
        def wrapper(context, *args, **kwargs):
            if test_func(context.user):
                return f(*args, **kwargs)
            return PermissionDenied()
        return wrapper
    return decorator


def permission_required(perm):
    def check_perms(user):
        if isinstance(perm, six.string_types):
            perms = (perm,)
        else:
            perms = perm

        if user.has_perms(perms):
            return True
        return False
    return user_passes_test(check_perms)
