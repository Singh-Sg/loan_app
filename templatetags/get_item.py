# https://stackoverflow.com/a/8000091 (how to access dictionary by variable inside template)
from django.template.defaulttags import register


@register.filter
def get_dict_item(dictionary, key):
    return dictionary.get(key)
