{
    "name": "InstaPunchout - cXML & OCI Punchout for eCommerce",
    "version": "19.0.1.0.0",
    "author": "InstaPunchout",
    "maintainer": "InstaPunchout",
    "support": "info@instapunchout.com",
    "website": "https://www.instapunchout.com",
    "summary": "Connect your Odoo webshop to your buyers' procurement systems (SAP Ariba, Coupa, SAP OCI...). "
               "Free connector - requires a paid InstaPunchout subscription (setup fee + yearly license per connection).",
    "category": "Website/eCommerce",
    "license": "LGPL-3",
    "depends": ["base", "website", "website_sale"],
    "external_dependencies": {"python": ["requests"]},
    "data": ["views.xml"],
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": True,
    "auto_install": False,
}
