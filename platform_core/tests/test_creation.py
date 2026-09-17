"""One-step application creation, and the features chosen while creating it.

Creating an application used to take three pages and three redirects, and the
application arrived with every feature silently on. These cover the combined
form: the hierarchy it creates, the validation that stops it filing things
somewhere unexpected, the feature rows it writes, and - importantly - that none
of it quietly widens access.
"""

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.models import (
    Application,
    ApplicationFeature,
    ApplicationGrant,
    AuditEvent,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    User,
)
from platform_core.services import available_features


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CLAUDE_USE_HOST_LOGIN=False,
)
class ApplicationCreationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("orgadmin")
        self.owner = User.objects.create_user("appowner")
        self.outsider = User.objects.create_user("outsider")
        self.org = Organization.objects.create(name="Creation")
        OrganizationMember.objects.create(organization=self.org, user=self.admin, is_admin=True)
        OrganizationMember.objects.create(organization=self.org, user=self.owner)
        self.other = Organization.objects.create(name="Elsewhere")
        OrganizationMember.objects.create(organization=self.other, user=self.admin, is_admin=True)
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")

    def url(self, organization=None):
        return reverse("application-create", args=[(organization or self.org).pk])

    def payload(self, **overrides):
        data = {
            "name": "Reporting",
            "new_portfolio": "Platform",
            "new_product": "Insights",
            "owner": str(self.owner.pk),
        }
        # Every feature checkbox ticked, which is what an untouched form posts.
        for key, _ in available_features():
            data[f"feature_{key}"] = "on"
        data.update(overrides)
        return {key: value for key, value in data.items() if value is not None}

    def test_one_submission_creates_portfolio_product_application_and_grant(self):
        response = self.client.post(self.url(), self.payload())
        self.assertEqual(response.status_code, 302)
        app = Application.objects.get(name="Reporting")
        self.assertEqual(app.product.name, "Insights")
        self.assertEqual(app.product.portfolio.name, "Platform")
        self.assertEqual(app.product.portfolio.organization, self.org)
        grant = ApplicationGrant.objects.get(application=app)
        self.assertEqual(grant.user, self.owner)
        self.assertEqual(grant.role, "owner")

    def test_each_created_level_is_audited(self):
        self.client.post(self.url(), self.payload())
        actions = set(AuditEvent.objects.values_list("action", flat=True))
        self.assertEqual(
            {"portfolio.created", "product.created", "application.created"} - actions, set()
        )

    def test_existing_portfolio_and_product_are_reused_not_duplicated(self):
        portfolio = Portfolio.objects.create(organization=self.org, name="Existing")
        product = Product.objects.create(portfolio=portfolio, name="Also existing")
        response = self.client.post(
            self.url(),
            self.payload(
                portfolio=str(portfolio.pk),
                product=str(product.pk),
                new_portfolio="",
                new_product="",
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Portfolio.objects.filter(organization=self.org).count(), 1)
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(Application.objects.get(name="Reporting").product, product)

    def test_an_existing_portfolio_may_take_a_new_product(self):
        portfolio = Portfolio.objects.create(organization=self.org, name="Existing")
        response = self.client.post(
            self.url(), self.payload(portfolio=str(portfolio.pk), new_portfolio="")
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Application.objects.get(name="Reporting").product.portfolio, portfolio)

    def test_naming_and_choosing_the_same_level_is_rejected(self):
        portfolio = Portfolio.objects.create(organization=self.org, name="Existing")
        response = self.client.post(
            self.url(), self.payload(portfolio=str(portfolio.pk), new_portfolio="Also named")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Application.objects.exists())
        self.assertContains(response, "Choose an existing portfolio or name a new one.")

    def test_neither_choosing_nor_naming_is_rejected(self):
        response = self.client.post(self.url(), self.payload(new_portfolio="", new_product=""))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Application.objects.exists())
        self.assertContains(response, "Choose a portfolio or name a new one.")

    def test_a_product_from_another_portfolio_is_rejected(self):
        first = Portfolio.objects.create(organization=self.org, name="First")
        second = Portfolio.objects.create(organization=self.org, name="Second")
        product = Product.objects.create(portfolio=second, name="Belongs to Second")
        response = self.client.post(
            self.url(),
            self.payload(
                portfolio=str(first.pk),
                product=str(product.pk),
                new_portfolio="",
                new_product="",
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "That product belongs to a different portfolio.")
        self.assertFalse(Application.objects.exists())

    def test_nothing_is_created_when_the_form_fails(self):
        """The whole chain is one transaction; a rejected form leaves no orphans."""
        self.client.post(self.url(), self.payload(owner=""))
        self.assertFalse(Portfolio.objects.filter(name="Platform").exists())
        self.assertFalse(Product.objects.exists())
        self.assertFalse(Application.objects.exists())

    def test_another_organizations_portfolio_cannot_be_selected(self):
        foreign = Portfolio.objects.create(organization=self.other, name="Foreign")
        response = self.client.post(
            self.url(), self.payload(portfolio=str(foreign.pk), new_portfolio="")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Application.objects.exists())

    def test_a_user_outside_the_organization_cannot_be_made_owner(self):
        response = self.client.post(self.url(), self.payload(owner=str(self.outsider.pk)))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Application.objects.exists())

    def test_each_plus_control_creates_the_next_level_down(self):
        """A "+" adds one level, never skips one.

        Organization adds a portfolio, portfolio adds a product, product adds an
        application. An organization-level "+" pointing at application creation
        would be two levels out and reads as if applications hang off the
        organization directly.
        """
        portfolio = Portfolio.objects.create(organization=self.org, name="Visible")
        product = Product.objects.create(portfolio=portfolio, name="Also visible")
        app = Application.objects.create(product=product, name="Anchor")
        # The sidebar tree is built from applications the viewer can reach, so a
        # grant is what makes the portfolio and product appear at all.
        ApplicationGrant.objects.create(application=app, user=self.admin, role="owner")

        page = self.client.get(reverse("organization", args=[self.org.pk]))
        body = page.content.decode()
        self.assertIn(f'href="{reverse("portfolio-new", args=[self.org.pk])}"', body)
        self.assertIn(f'href="{reverse("product-new", args=[portfolio.pk])}"', body)
        self.assertIn(f'href="{reverse("application-new", args=[product.pk])}"', body)

        # The one-step create spans three levels, so it must not wear a bare "+".
        self.assertNotIn("+ New application", body)
        self.assertIn(reverse("application-create", args=[self.org.pk]), body)

    def test_the_overview_does_not_restate_the_sidebar_hierarchy(self):
        """The hierarchy lives in one place.

        The sidebar tree lists every organization, links to the same pages, nests
        portfolios and products underneath and carries the "+" controls. A second
        flat list on the Overview screen said less about the same thing, so it is
        gone - but the tree's own link must still be there, or the organization
        page becomes unreachable from the welcome screen.
        """
        portfolio = Portfolio.objects.create(organization=self.org, name="Visible")
        product = Product.objects.create(portfolio=portfolio, name="Also visible")
        app = Application.objects.create(product=product, name="Anchor")
        ApplicationGrant.objects.create(application=app, user=self.admin, role="owner")
        body = self.client.get(reverse("dashboard")).content.decode()
        # The removed markup specifically, not the word: the page still points at
        # the sidebar in prose, and the metric still counts memberships.
        self.assertNotIn("<h2>Your organizations</h2>", body)
        self.assertNotIn("View organization", body)
        # Still reachable, via the sidebar tree that renders on every page.
        self.assertIn(f'href="{reverse("organization", args=[self.org.pk])}"', body)
        self.assertIn('class="org-tree"', body)

    def test_the_overview_still_explains_an_empty_workspace(self):
        """Removing the list must not remove the only "you have nothing" guidance."""
        stranger = User.objects.create_user("stranger")
        self.client.force_login(stranger, backend="django.contrib.auth.backends.ModelBackend")
        body = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("No organization membership yet", body)

    def test_the_tree_plus_controls_are_hidden_from_non_admins(self):
        portfolio = Portfolio.objects.create(organization=self.org, name="Visible")
        product = Product.objects.create(portfolio=portfolio, name="Also visible")
        app = Application.objects.create(product=product, name="Anchor")
        ApplicationGrant.objects.create(application=app, user=self.owner, role="owner")
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        body = self.client.get(reverse("organization", args=[self.org.pk])).content.decode()
        # A "+" here would open a popup onto a 404: creating needs org admin.
        for name, args in (
            ("portfolio-new", [self.org.pk]),
            ("product-new", [portfolio.pk]),
            ("application-new", [product.pk]),
        ):
            with self.subTest(route=name):
                self.assertNotIn(f'href="{reverse(name, args=args)}"', body)

    def test_the_popup_targets_are_real_pages(self):
        """The progressive-enhancement contract for the new popups.

        static/modal.js only intercepts: it fetches the href and lifts <main> out
        of the response. So every data-modal link has to be a page that renders
        and submits on its own, which is also exactly what happens with
        JavaScript switched off.
        """
        page = self.client.get(reverse("organization", args=[self.org.pk]))
        self.assertContains(page, "data-modal")
        for name, args in (
            ("application-create", [self.org.pk]),
            ("portfolio-new", [self.org.pk]),
            ("organization-members", [self.org.pk]),
        ):
            with self.subTest(route=name):
                response = self.client.get(reverse(name, args=args))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "<form")
                self.assertContains(response, "csrfmiddlewaretoken")

    def test_the_standalone_page_creates_exactly_as_the_popup_does(self):
        """No separate code path for the popup: same URL, same view, same result."""
        response = self.client.post(self.url(), self.payload(), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Application.objects.filter(name="Reporting").exists())

    def test_a_non_admin_member_cannot_create(self):
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.post(self.url(), self.payload())
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Application.objects.exists())

    def test_creating_in_an_unrelated_organization_is_denied(self):
        stranger = Organization.objects.create(name="Stranger")
        response = self.client.post(self.url(stranger), self.payload())
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Application.objects.exists())


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CLAUDE_USE_HOST_LOGIN=False,
)
class FeatureSelectionTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("orgadmin")
        self.owner = User.objects.create_user("appowner")
        self.org = Organization.objects.create(name="Features")
        OrganizationMember.objects.create(organization=self.org, user=self.admin, is_admin=True)
        OrganizationMember.objects.create(organization=self.org, user=self.owner)
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")

    def create(self, **features):
        data = {
            "name": "Selective",
            "new_portfolio": "P",
            "new_product": "Q",
            "owner": str(self.owner.pk),
            # The marker the form posts: without it an absent checkbox means
            # "this caller said nothing about features", not "switch it off".
            "features_declared": "1",
        }
        for key, _ in available_features():
            if features.get(key, True):
                data[f"feature_{key}"] = "on"
        response = self.client.post(reverse("application-create", args=[self.org.pk]), data)
        self.assertEqual(response.status_code, 302)
        return Application.objects.get(name="Selective")

    def test_every_registry_feature_appears_on_the_form(self):
        response = self.client.get(reverse("application-create", args=[self.org.pk]))
        for key, label in available_features():
            self.assertContains(response, f"feature_{key}")
            self.assertContains(response, label)

    def test_all_features_ticked_writes_no_rows(self):
        """A missing row already means enabled; writing them all would be noise."""
        app = self.create()
        self.assertFalse(ApplicationFeature.objects.filter(application=app).exists())

    def test_a_caller_that_says_nothing_about_features_gets_the_defaults(self):
        """Guards the older application-new route, and any script posting by hand.

        An unticked checkbox is simply absent from the POST, so "all off" and
        "never mentioned" are the same bytes. Without the marker the defaults
        must win, or an application would silently arrive with nothing enabled.

        The defaults are "everything except the opt-in features", so those are
        the only rows a silent caller produces.
        """
        from platform_core.services import OPT_IN_FEATURES

        response = self.client.post(
            reverse("application-create", args=[self.org.pk]),
            {
                "name": "Untouched",
                "new_portfolio": "P",
                "new_product": "Q",
                "owner": str(self.owner.pk),
            },
        )
        self.assertEqual(response.status_code, 302)
        app = Application.objects.get(name="Untouched")
        written = dict(
            ApplicationFeature.objects.filter(application=app).values_list("key", "enabled")
        )
        self.assertEqual(set(written), OPT_IN_FEATURES)
        self.assertFalse(any(written.values()))

    def test_declaring_features_with_none_ticked_disables_them_all(self):
        """The other half of the marker: an explicit "all off" is honoured."""
        response = self.client.post(
            reverse("application-create", args=[self.org.pk]),
            {
                "name": "Bare",
                "new_portfolio": "P",
                "new_product": "Q",
                "owner": str(self.owner.pk),
                "features_declared": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        app = Application.objects.get(name="Bare")
        rows = ApplicationFeature.objects.filter(application=app, enabled=False)
        self.assertEqual(rows.count(), len(available_features()))

    def test_an_unticked_feature_is_recorded_as_disabled(self):
        app = self.create(code_factory=False)
        row = ApplicationFeature.objects.get(application=app, key="code_factory")
        self.assertFalse(row.enabled)
        self.assertEqual(ApplicationFeature.objects.filter(application=app).count(), 1)

    def test_a_feature_unticked_at_creation_is_enforced(self):
        app = self.create(chat=False)
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(reverse("chat", args=[app.pk])).status_code, 403)

    def test_a_feature_left_ticked_at_creation_still_works(self):
        app = self.create(chat=False)
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(reverse("graph", args=[app.pk])).status_code, 200)

    def test_the_disabled_feature_disappears_from_the_navigation(self):
        app = self.create(code_factory=False)
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.get(reverse("graph", args=[app.pk]))
        self.assertNotContains(response, ">Code Factory</a>")
        self.assertContains(response, ">Chat</a>")


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CLAUDE_USE_HOST_LOGIN=False,
)
class FeatureScreenTests(TestCase):
    """The Features screen saves every switch in one submit."""

    def setUp(self):
        self.owner = User.objects.create_user("appowner")
        self.org = Organization.objects.create(name="Screen")
        OrganizationMember.objects.create(organization=self.org, user=self.owner, is_admin=True)
        portfolio = Portfolio.objects.create(organization=self.org, name="P")
        product = Product.objects.create(portfolio=portfolio, name="Q")
        self.app = Application.objects.create(product=product, name="Switched")
        ApplicationGrant.objects.create(application=self.app, user=self.owner, role="owner")
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        self.url = reverse("application-features", args=[self.app.pk])

    def test_one_submit_saves_several_switches(self):
        keys = [key for key, _ in available_features()]
        keep = {f"feature_{key}": "on" for key in keys if key not in {"chat", "code_factory"}}
        response = self.client.post(self.url, {"features_declared": "1", **keep})
        self.assertEqual(response.status_code, 302)
        disabled = set(
            ApplicationFeature.objects.filter(application=self.app, enabled=False).values_list(
                "key", flat=True
            )
        )
        self.assertEqual(disabled, {"chat", "code_factory"})

    def test_only_real_changes_are_audited(self):
        """Saving an unchanged page should not write a pile of audit events."""
        everything = {f"feature_{key}": "on" for key, _ in available_features()}
        self.client.post(self.url, {"features_declared": "1", **everything})
        self.assertFalse(AuditEvent.objects.filter(action__startswith="feature.").exists())

    def test_re_enabling_is_audited_and_takes_effect(self):
        self.client.post(self.url, {"features_declared": "1"})
        self.assertTrue(AuditEvent.objects.filter(action="feature.disabled").exists())
        self.assertEqual(self.client.get(reverse("chat", args=[self.app.pk])).status_code, 403)
        everything = {f"feature_{key}": "on" for key, _ in available_features()}
        self.client.post(self.url, {"features_declared": "1", **everything})
        self.assertTrue(AuditEvent.objects.filter(action="feature.enabled").exists())
        self.assertEqual(self.client.get(reverse("chat", args=[self.app.pk])).status_code, 200)

    def test_a_disabled_feature_answers_403_everywhere(self):
        """chat_settings used to answer 404 while workbench.access answered 403."""
        self.client.post(self.url, {"features_declared": "1"})
        for name in ("chat-settings", "usage", "chat"):
            with self.subTest(route=name):
                response = self.client.get(reverse(name, args=[self.app.pk]))
                self.assertEqual(response.status_code, 403)


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CLAUDE_USE_HOST_LOGIN=False,
)
class CreatorAccessTests(TestCase):
    """Deny-by-default must survive the convenience of a self-grant checkbox."""

    def setUp(self):
        self.admin = User.objects.create_user("orgadmin")
        self.owner = User.objects.create_user("appowner")
        self.org = Organization.objects.create(name="Access")
        OrganizationMember.objects.create(organization=self.org, user=self.admin, is_admin=True)
        OrganizationMember.objects.create(organization=self.org, user=self.owner)
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")

    def create(self, **extra):
        data = {
            "name": "Guarded",
            "new_portfolio": "P",
            "new_product": "Q",
            "owner": str(self.owner.pk),
        }
        data.update(extra)
        self.client.post(reverse("application-create", args=[self.org.pk]), data)
        return Application.objects.get(name="Guarded")

    def test_the_creating_admin_gets_no_access_by_default(self):
        app = self.create()
        self.assertFalse(ApplicationGrant.objects.filter(application=app, user=self.admin).exists())
        # Administering an organization still implies nothing about its contents.
        self.assertEqual(self.client.get(reverse("graph", args=[app.pk])).status_code, 404)

    def test_the_self_grant_is_recorded_explicitly_when_asked_for(self):
        app = self.create(grant_me_owner="on")
        grant = ApplicationGrant.objects.get(application=app, user=self.admin)
        self.assertEqual(grant.role, "owner")
        # Approval rights are a separate permission and are never included.
        self.assertFalse(grant.can_approve)
        self.assertEqual(self.client.get(reverse("graph", args=[app.pk])).status_code, 200)

    def test_asking_for_a_self_grant_as_the_named_owner_makes_only_one(self):
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        self.client.post(
            reverse("application-create", args=[self.org.pk]),
            {
                "name": "Guarded",
                "new_portfolio": "P",
                "new_product": "Q",
                "owner": str(self.admin.pk),
                "grant_me_owner": "on",
            },
        )
        app = Application.objects.get(name="Guarded")
        self.assertEqual(ApplicationGrant.objects.filter(application=app).count(), 1)


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CLAUDE_USE_HOST_LOGIN=False,
)
class StructureVisibilityTests(TestCase):
    """What an organization's shape looks like before anything is in it.

    The sidebar tree was assembled purely from applications, so a portfolio
    holding nothing yet could not appear: an admin created one, the sidebar did
    not change, and the create looked as though it had failed. Empty levels are
    exactly the ones whose "+" is needed next, so they are the ones that must
    show.
    """

    def setUp(self):
        self.admin = User.objects.create_user("structure-admin")
        self.member = User.objects.create_user("structure-member")
        self.org = Organization.objects.create(name="Shape")
        OrganizationMember.objects.create(organization=self.org, user=self.admin, is_admin=True)
        OrganizationMember.objects.create(organization=self.org, user=self.member)
        self.empty = Portfolio.objects.create(organization=self.org, name="HealthCare")
        self.filled = Portfolio.objects.create(organization=self.org, name="Middleware")
        self.product = Product.objects.create(portfolio=self.filled, name="ACEandMQ")
        self.barren = Product.objects.create(portfolio=self.filled, name="EventStreams")
        self.app = Application.objects.create(product=self.product, name="MQACEKnowledge")
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")

    def tree(self, user=None):
        if user is not None:
            self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        body = self.client.get(reverse("organization", args=[self.org.pk])).content.decode()
        return body.split('aria-label="Organization tree"')[1].split("</nav>")[0]

    def test_a_portfolio_holding_nothing_still_appears_in_the_tree(self):
        self.assertIn("HealthCare", self.tree())

    def test_a_product_holding_nothing_still_appears_in_the_tree(self):
        self.assertIn("EventStreams", self.tree())

    def test_an_empty_portfolio_carries_the_control_that_fills_it(self):
        self.assertIn(reverse("product-new", args=[self.empty.pk]), self.tree())

    def test_the_tree_still_withholds_an_application_without_a_grant(self):
        """Scaffolding is the organization's shape. An application is not.

        An admin sees portfolios and products because those are the
        organization's own structure and the organization page already lists
        them. Application names still arrive only through a grant.
        """
        self.assertNotIn(reverse("application", args=[self.app.pk]), self.tree())
        ApplicationGrant.objects.create(application=self.app, user=self.admin, role="owner")
        self.assertIn(reverse("application", args=[self.app.pk]), self.tree())

    def test_a_member_who_does_not_administer_sees_no_scaffolding(self):
        """A plain member's tree is still built from what they were granted."""
        tree = self.tree(user=self.member)
        self.assertNotIn("HealthCare", tree)
        self.assertNotIn("EventStreams", tree)

    def test_the_structure_panel_names_each_level(self):
        body = self.client.get(reverse("organization", args=[self.org.pk])).content.decode()
        panel = body.split('class="card section hierarchy"')[1]
        for kind in ("Portfolio", "Product", "Application"):
            self.assertIn(f'<span class="level-kind">{kind}</span>', panel)

    def test_the_structure_panel_says_what_an_empty_level_needs_next(self):
        body = self.client.get(reverse("organization", args=[self.org.pk])).content.decode()
        self.assertIn("No products yet", body)
        self.assertIn("No applications yet", body)

    def test_an_application_the_admin_cannot_open_is_marked_and_not_linked(self):
        """Administering an organization does not grant access to what is in it.

        The panel names the application because the admin manages the structure,
        and refuses to link it because the link would land on a 404 - the same
        rule, shown rather than hidden.
        """
        body = self.client.get(reverse("organization", args=[self.org.pk])).content.decode()
        self.assertIn("MQACEKnowledge", body)
        self.assertIn("No access", body)
        self.assertNotIn(f'href="{reverse("application", args=[self.app.pk])}"', body)

        ApplicationGrant.objects.create(application=self.app, user=self.admin, role="owner")
        body = self.client.get(reverse("organization", args=[self.org.pk])).content.decode()
        self.assertIn(f'href="{reverse("application", args=[self.app.pk])}"', body)
        self.assertNotIn("No access", body)
