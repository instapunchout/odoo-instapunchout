# -*- coding: utf-8 -*-
import json
import logging

import requests
import werkzeug
from odoo import fields, http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

CLOUD = "https://punchout.cloud"
TIMEOUT = 30
NOT_IN_SESSION = json.dumps({"message": "You're not in a punchout session."})


def post_cloud(path, data):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json", "Cache-Control": "no-cache"}
    return requests.post(CLOUD + path, data=json.dumps(data), headers=headers, timeout=TIMEOUT)


def cart_order(user):
    # the punchout cart: clearing, cart edit restore and cart return all use this same order
    # (draft only; sudo: portal record rules hide some of the user's own website carts,
    # the partner filter keeps it to this user)
    return request.env["sale.order"].sudo().search(
        [("partner_id", "=", user.partner_id.id), ("state", "=", "draft")],
        order="write_date desc",
        limit=1,
    )


def cart_json(user):
    order = cart_order(user)
    return {
        "website_id": order.website_id.id,
        "partner_id": user.partner_id.id,
        "pricelist_id": order.pricelist_id.id or None,
        "amount_untaxed": order.amount_untaxed,
        "amount_tax": order.amount_tax,
        "amount_total": order.amount_total,
        "currency_rate": order.currency_rate,
        "id": order.id,
        "currency": order.currency_id.name,
        "amount_delivery": order.amount_delivery,
        "amount_undiscounted": order.amount_undiscounted if "amount_undiscounted" in order._fields else None,
        "carrier_id": order.carrier_id.id or None,
        "comment": order.note or None,
        "commitment_date": order.commitment_date.strftime("%d-%m-%Y") if order.commitment_date else None,
        "lines": [{
            "name": p.name,
            "price_unit": p.price_unit,
            "price_tax": p.price_tax,
            "price_subtotal": p.price_subtotal,
            "price_total": p.price_total,
            "price_reduce_taxinc": p.price_reduce_taxinc,
            "price_reduce_taxexcl": p.price_reduce_taxexcl,
            "discount": p.discount,
            "product_id": p.product_id.id,
            "product_code": p.product_id.code,
            "product_uom_qty": p.product_uom_qty,
            "unspsc": (p.product_id.x_studio_unspsc_code if "x_studio_unspsc_code" in p.product_id._fields else False)
            or (p.product_id.unspsc_code if "unspsc_code" in p.product_id._fields else False) or None,
            "is_delivery": p.is_delivery if "is_delivery" in p._fields else None,
        } for p in order.order_line],
    }


def finalize_session():
    # Odoo 20 moved Session.finalize to a module-level function
    session = request.session
    if hasattr(session, "finalize"):
        session.finalize(request.env)
    else:
        from odoo.http.session import finalize
        finalize(session, request.env)


def find_user(Users, email, website_id):
    # logins are only unique per website (and may differ in case), so several users can match:
    # prefer the connection's website, then users without a website, then the most recent one
    pattern = email.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    users = Users.search([("login", "=ilike", pattern)])
    if len(users) > 1:
        _logger.warning("punchout: %s users with login %s: %s", len(users), email, users.ids)

    def rank(user):
        if website_id and user.website_id.id == int(website_id):
            return (0, -user.id)
        return (1 if not user.website_id else 2, -user.id)

    return users.sorted(key=rank)[:1]


def upsert_user(data):
    Users = request.env["res.users"].sudo()
    user = find_user(Users, data["email"], data.get("website_id"))
    store_id = data.get("store_id")

    if user:
        if store_id and user.company_id.id != store_id:
            # existing user in another company: move them to the connection's company
            try:
                with request.env.cr.savepoint():
                    user.write({"company_ids": [(4, store_id)], "company_id": store_id})
                _logger.info("Moved user %s to company %s", user.id, store_id)
            except Exception:
                _logger.exception("punchout: moving user to company failed")
        return user

    if data["account_create_disabled"]:
        return None

    _logger.info("Creating New Customer")
    partner = request.env["res.partner"].sudo().search(
        [("email", "=", data["email"]), ("parent_id", "=", data["company_id"])])
    if not partner:
        partner = request.env["res.partner"].sudo().create({
            "name": data["name"],
            "email": data["email"],
            "parent_id": data["company_id"],
            "commercial_partner_id": data["company_id"],
        })

    new_user = {
        "login": data["email"],
        "email": data["email"],
        "password": data["password"],
        "new_password": data["password"],
        "active": True,
        "company_type": "person",
        "partner_id": partner.id,
        "commercial_partner_id": partner.id,
        # Odoo 19 renamed res.users.groups_id to group_ids
        "group_ids" if "group_ids" in Users._fields else "groups_id": [(6, 0, [request.env.ref("base.group_portal").id])],
    }
    if data.get("website_id"):
        new_user["website_id"] = data["website_id"]
    if store_id:
        new_user["company_id"] = store_id
        new_user["company_ids"] = [(6, 0, [store_id])]

    # Odoo 20 no longer exposes some partner fields on res.users
    new_user = {key: value for key, value in new_user.items() if key in Users._fields}
    user = Users.with_context(signup_valid=True).create(new_user)
    _logger.info("Customer Created")
    return user


class Instapunchout(http.Controller):

    @http.route("/punchout/getcart", auth="public", csrf=False)
    def getcart(self, **kw):
        # only inside a punchout session (the public user would see other visitors' carts)
        if "PunchoutId" not in request.session:
            return NOT_IN_SESSION
        return json.dumps(cart_json(request.env.user))

    @http.route("/punchout", auth="public", csrf=False)
    def index(self, **kw):
        path = request.httprequest.args.get("path", "")
        punchout_id = request.session.get("PunchoutId")

        if path == "companies.json":
            return "Not yet implemented"

        if path == "script.js":
            script = "window.instalogs = []; console.ilog = (...args)=> window.instalogs.push(...args);\n"
            if not punchout_id:
                script += "console.ilog(\"You're not in a punchout session.\")"
            else:
                try:
                    script += requests.get(CLOUD + "/punchout.js?id=" + punchout_id, timeout=10).text
                except Exception:
                    _logger.exception("punchout: loading punchout.js failed")
                    script += "console.ilog(\"Could not load punchout.js\")"
            return Response(script, content_type="application/javascript")

        if path == "cart.json":
            if not punchout_id:
                return Response(NOT_IN_SESSION, content_type="application/json")
            return Response(json.dumps(cart_json(request.env.user)), content_type="application/json")

        if path == "cart":
            if not punchout_id:
                return Response(NOT_IN_SESSION, content_type="text/plain")
            try:
                custom = json.loads(request.httprequest.data)
            except Exception:
                custom = {}
            data = {"cart": {"Odoo": cart_json(request.env.user)}, "custom": custom}
            return Response(json.dumps(post_cloud("/cart/" + punchout_id, data).json()), content_type="text/plain")

        # punchout setup: InstaPunchout decides what to do with this request
        headers = dict(request.httprequest.headers.to_wsgi_list())
        res = post_cloud("/proxy", {"body": str(request.httprequest.data),
                                    "query": request.httprequest.args, "headers": headers, "server": {}})
        try:
            resp = res.json()
        except Exception:
            return res.text

        if resp.get("action") != "login":
            # "print": error / info page from InstaPunchout (expired or invalid session)
            return Response(resp.get("body") or res.text, content_type="text/html")

        # no company_id when account creation is disabled: existing users are looked up by email
        if resp.get("company_id") and not request.env["res.partner"].sudo().browse(resp["company_id"]).exists():
            return "Partner id " + str(resp["company_id"]) + " does not exist, Please contact your supplier to add the new id"

        email = resp["email"]
        # punchout only ever logs in portal users: never internal or admin accounts
        existing = find_user(request.env["res.users"].sudo(), email, resp.get("website_id"))
        if existing and not existing.has_group("base.group_portal"):
            _logger.warning("punchout: refused login of non-portal user %s", existing.id)
            return Response("This account cannot be used for punchout because it is not a portal user. "
                            "Please contact your supplier.", status=403, content_type="text/plain")

        request.session["PunchoutId"] = resp["punchout_id"]
        user = upsert_user(resp)
        if not user:
            if not resp["account_create_disabled"]:
                return "Failed to get user with email " + email + " , Please contact your supplier"
            return "User creation is disabled and not user with email " + email + " was found , Please contact your supplier"

        uid = user.id
        _logger.info("Punchout login user %s", uid)

        website_id = resp.get("website_id")
        website = request.env["website"].sudo().browse(int(website_id)) if website_id else None
        if website and website.exists():
            request.website = website
            request.session["website_id"] = website.id

        request.session["pre_uid"] = uid
        request.session["pre_login"] = email

        user = request.env(user=uid)["res.users"].browse(uid)
        if not user._mfa_url():
            finalize_session()

        # clear cart (as the punchout user: request.env.user is still the public user here)
        order = cart_order(user)
        if order:
            order.website_order_line.unlink()

        # cart edit: restore the lines of the cart being edited
        cart_items = (resp.get("properties") or {}).get("cart_items") or []
        if cart_items:
            try:
                with request.env.cr.savepoint():
                    if not (website and website.exists()):
                        website = request.env["website"].get_current_website()
                    order = order or request.env["sale.order"].sudo().create(
                        {"partner_id": user.partner_id.id, "website_id": website.id})
                    order.order_line = [(0, 0, {"product_id": int(i["product_id"]),
                                                "product_uom_qty": float(i["quantity"])}) for i in cart_items]
                    order.date_order = fields.Datetime.now()
                request.session["sale_order_id"] = order.id
            except Exception:
                _logger.exception("punchout: restoring edited cart failed")

        return werkzeug.utils.redirect(resp.get("redirect") or "/")
