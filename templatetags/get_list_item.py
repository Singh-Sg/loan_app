# https://stackoverflow.com/a/29664945/8211573 (how to access list element by index variable)
from django import template
register = template.Library()


@register.filter
def get_list_item(lst, i):
    return lst[int(i)]
