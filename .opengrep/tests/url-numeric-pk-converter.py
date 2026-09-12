from django.urls import path
from django.urls import path as route
import django.urls


urlpatterns = [
    # ruleid: url-numeric-pk-converter
    path("devices/<str:pk>/", view),
    # ok: url-numeric-pk-converter
    path("ports/<str:id>/", view),
    # ruleid: url-numeric-pk-converter
    route("devices/<str:pk>/", view),
    # ok: url-numeric-pk-converter
    django.urls.path("ports/<str:id>/", view),
    # ruleid: url-numeric-pk-converter
    django.urls.path("devices/<str:pk>/", view),
    # ruleid: url-numeric-pk-converter
    django.urls.path(route="devices/<str:pk>/", view=view),
    # ruleid: url-numeric-pk-converter
    path(route="devices/<str:pk>/", view=view),
    # ok: url-numeric-pk-converter
    path("devices/<int:pk>/", view),
    # ok: url-numeric-pk-converter
    path("devices/<int:id>/", view),
    # ok: url-numeric-pk-converter
    path("devices/<str:slug>/", view),
]

# ok: url-numeric-pk-converter
example = "<str:pk>"


STRING_PK_ROUTE = "devices/<str:pk>/"
IMPLICIT_PK_ROUTE = "devices/<pk>/"
NUMERIC_PK_ROUTE = "devices/<int:pk>/"
urlpatterns += [
    # ruleid: url-numeric-pk-converter
    path("devices/<pk>/", view),
    # ruleid: url-numeric-pk-converter
    path(STRING_PK_ROUTE, view),
    # ruleid: url-numeric-pk-converter
    path(route=IMPLICIT_PK_ROUTE, view=view),
    # ok: url-numeric-pk-converter
    path(NUMERIC_PK_ROUTE, view),
    # ok: url-numeric-pk-converter
    path("devices/<str:package>/", view),
]



def dynamic_routes(prefix):
    # The rule cannot evaluate this route builder.
    # ok: url-numeric-pk-converter
    return path(build_route(prefix, "pk"), view)
