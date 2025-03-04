from mountaineer.app import AppController
from mountaineer.js_compiler.postcss import PostCSSBundler
from mountaineer.render import LinkAttribute, Metadata

{% if create_stub_files %}
from {{project_name}}.controllers.detail import DetailController
from {{project_name}}.controllers.home import HomeController
{% endif %}
from {{project_name}}.views import get_view_path
from {{project_name}}.config import AppConfig

controller = AppController(
    view_root=get_view_path(""),
    config=AppConfig(),
    {% if use_tailwind %}
    global_metadata=Metadata(
        links=[LinkAttribute(rel="stylesheet", href="/static/app_main.css")]
    ),
    custom_builders=[
        PostCSSBundler(),
    ],
    {% endif %}
)

{% if create_stub_files %}
controller.register(HomeController())
controller.register(DetailController())
{% endif %}
