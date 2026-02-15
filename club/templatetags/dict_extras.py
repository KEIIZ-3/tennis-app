from django import template

register = template.Library()

@register.filter
def get_item(dct, key):
    if not dct:
        return []
    return dct.get(key, [])
