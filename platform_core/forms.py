import io
import warnings

from django import forms
from django.db.models import Q
from PIL import Image, UnidentifiedImageError

from .models import ApplicationGrant, ChatRetention, Portfolio, Product, User
from .services import (
    AREA_ICONS,
    OPT_IN_FEATURES,
    PURPOSE_CHOICES,
    area_of,
    available_features,
    features_for_purpose,
)


class ResourceForm(forms.Form):
    name = forms.CharField(max_length=120, label="Name")


class AdministratorsField(forms.ModelMultipleChoiceField):
    widget = forms.CheckboxSelectMultiple

    def label_from_instance(self, user):
        name = user.get_full_name()
        return f"{user.username} ({name})" if name else user.username


class OrganizationAdministratorsForm(forms.Form):
    """Who administers an organization. One is required, several are allowed.

    Choices are active users, plus anyone already an administrator of this
    organization: a deactivated admin who is not offered would silently drop out
    of the posted set and be demoted by a save that never mentioned them.
    """

    administrators = AdministratorsField(
        queryset=User.objects.none(),
        label="Organization administrators",
        help_text="Choose one or more existing users. Administering an organization "
        "does not grant access to its applications.",
        error_messages={"required": "An organization needs at least one administrator."},
    )

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        eligible = Q(is_active=True)
        if organization is not None:
            eligible |= Q(
                organizationmember__organization=organization,
                organizationmember__is_admin=True,
            )
        self.fields["administrators"].queryset = (
            User.objects.filter(eligible).distinct().order_by("username")
        )


class OrganizationForm(ResourceForm, OrganizationAdministratorsForm):
    pass


class MemberForm(forms.Form):
    username = forms.CharField(max_length=150, label="Existing user ID")


class ApplicationForm(ResourceForm):
    """Everything a new application needs, on one form.

    Creating an application used to mean three pages - portfolio, then product,
    then application - each redirecting back to the organization page, where the
    controls sat behind a collapsed disclosure. The hierarchy is unchanged in the
    database; it just stops costing three trips. Either level may be an existing
    row or a new name, and the features the application starts with are chosen
    here rather than discovered afterwards on another screen.
    """

    portfolio = forms.ModelChoiceField(
        queryset=Portfolio.objects.none(),
        required=False,
        label="Portfolio",
        help_text="Choose one, or leave blank and name a new portfolio below.",
    )
    new_portfolio = forms.CharField(max_length=120, required=False, label="New portfolio name")
    product = forms.ModelChoiceField(
        queryset=Product.objects.none(),
        required=False,
        label="Product",
        help_text="Choose one, or leave blank and name a new product below.",
    )
    new_product = forms.CharField(max_length=120, required=False, label="New product name")
    owner = forms.ModelChoiceField(queryset=User.objects.none(), label="Application owner")
    # Asked on the page (the radios carry `required`), tolerated when absent so a
    # caller that predates the question still creates what it always did.
    purpose = forms.ChoiceField(
        choices=[(key, label) for key, label, _ in PURPOSE_CHOICES],
        required=False,
        label="What is this application for?",
        widget=forms.RadioSelect(attrs={"required": True}),
    )
    # Ticked by default. Approval can only be handed on by someone who holds it
    # (`services.change_grant`), so an owner created without it could never give
    # it to anyone, themselves included, and Code Factory stalled at review with
    # no way out through the UI. Unticking it still records an owner without it.
    # Holding it does not let anyone approve their own plan: `review_plan`
    # refuses that unless `allow_self_approval` is set.
    owner_can_approve = forms.BooleanField(
        required=False,
        initial=True,
        label="Grant this owner Code Factory approval rights",
        help_text="An owner without approval rights cannot give them to anyone later.",
    )
    grant_me_owner = forms.BooleanField(
        required=False,
        label="Also grant me owner access to this application",
        help_text=(
            "Administering an organization does not grant access to its applications. "
            "Tick this to record an explicit grant for yourself."
        ),
    )

    def __init__(self, *args, organization, product=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.fields["owner"].queryset = User.objects.filter(
            is_active=True, organizationmember__organization=organization
        )
        self.fields["portfolio"].queryset = Portfolio.objects.filter(organization=organization)
        self.fields["product"].queryset = Product.objects.filter(
            portfolio__organization=organization
        )
        if product is not None:
            # Reached from a specific product's "+", so both levels are decided.
            for name in ("portfolio", "new_portfolio", "product", "new_product"):
                del self.fields[name]
        # Features default to on, matching services.all_features, where a missing
        # row means enabled. Unticking one writes an ApplicationFeature row. Only
        # the shared ones are boxes: engineering and operations features follow
        # `purpose`, and connector kinds are chosen during onboarding.
        for key, label in available_features():
            if area_of(key) == "shared":
                self.fields[f"feature_{key}"] = forms.BooleanField(
                    required=False, initial=key not in OPT_IN_FEATURES, label=label
                )

    @property
    def purpose_cards(self):
        """Each purpose radio with its description and icon, for the cards."""
        described = {key: text for key, _, text in PURPOSE_CHOICES}
        return [
            (radio, described[radio.data["value"]], AREA_ICONS[radio.data["value"]])
            for radio in self["purpose"]
        ]

    @property
    def feature_fields(self):
        """The feature checkboxes, so the template can group them in a fieldset."""
        return [self[name] for name in self.fields if name.startswith("feature_")]

    def clean(self):
        values = super().clean()
        if "portfolio" not in self.fields:
            return values
        portfolio, new_portfolio = values.get("portfolio"), (values.get("new_portfolio") or "")
        product, new_product = values.get("product"), (values.get("new_product") or "")
        if portfolio and new_portfolio.strip():
            self.add_error("new_portfolio", "Choose an existing portfolio or name a new one.")
        if not portfolio and not new_portfolio.strip():
            self.add_error("portfolio", "Choose a portfolio or name a new one.")
        if product and new_product.strip():
            self.add_error("new_product", "Choose an existing product or name a new one.")
        if not product and not new_product.strip():
            self.add_error("product", "Choose a product or name a new one.")
        # A chosen product fixes the portfolio; disagreeing with the chosen
        # portfolio would silently file the application somewhere unexpected.
        if product and portfolio and product.portfolio_id != portfolio.pk:
            self.add_error("product", "That product belongs to a different portfolio.")
        if product and new_portfolio.strip():
            self.add_error("product", "An existing product already has a portfolio.")
        return values

    def selected_features(self):
        """{feature key: enabled} for every feature offered on this form.

        An unticked checkbox simply is not posted, so "every box off" and "this
        caller knows nothing about features" look identical on the wire - and the
        second must not disable everything. The template posts a hidden marker to
        tell them apart: without it the defaults stand, with it the checkboxes are
        taken literally, including all of them being off.
        """
        if not self.data.get("features_declared"):
            chosen = {
                key: key not in OPT_IN_FEATURES
                for key, _ in available_features()
                if area_of(key) == "shared"
            }
        else:
            chosen = {
                name.removeprefix("feature_"): bool(value)
                for name, value in self.cleaned_data.items()
                if name.startswith("feature_")
            }
        # No purpose posted - a script, or a caller from before the question
        # existed - is "Both", so nothing is switched off that used to be on.
        purpose = getattr(self, "cleaned_data", {}).get("purpose") or self.data.get("purpose")
        return chosen | features_for_purpose(purpose)


class GrantForm(forms.Form):
    user = forms.ModelChoiceField(queryset=User.objects.none())
    role = forms.ChoiceField(choices=ApplicationGrant.Role.choices)
    can_approve = forms.BooleanField(required=False, label="May approve Code Factory plans")

    def __init__(self, *args, application, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = User.objects.filter(
            is_active=True, organizationmember__organization_id=application.organization_id
        )


class BrandingForm(forms.Form):
    logo = forms.FileField(label="New platform logo", help_text="PNG, JPEG or WebP. Up to 2 MB.")

    def clean_logo(self):
        upload = self.cleaned_data["logo"]
        if upload.size > 2 * 1024 * 1024:
            raise forms.ValidationError("Logo must be 2 MB or smaller.")
        raw = upload.read(2 * 1024 * 1024 + 1)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as source:
                    if source.format not in {"PNG", "JPEG", "WEBP"}:
                        raise forms.ValidationError("Use a PNG, JPEG or WebP image.")
                    width, height = source.size
                    if width * height > 4_000_000 or max(width, height) > 4096:
                        raise forms.ValidationError(
                            "Logo must be at most 4 MP and 4096 px per side."
                        )
                    if getattr(source, "n_frames", 1) != 1:
                        raise forms.ValidationError("Animated images are not supported.")
                    source.load()
                    # Fresh canvas strips metadata and trailing/polyglot payloads, preserving alpha.
                    clean = Image.new("RGBA", source.size)
                    clean.paste(source.convert("RGBA"))
                    output = io.BytesIO()
                    clean.save(output, format="PNG", optimize=True)
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            raise forms.ValidationError("Upload a valid, undamaged image.") from None
        result = output.getvalue()
        if len(result) > 4 * 1024 * 1024:
            raise forms.ValidationError("Decoded logo is too large.")
        return result


class ChatRetentionForm(forms.ModelForm):
    """How many days of chat history to keep. 0 keeps it indefinitely."""

    class Meta:
        model = ChatRetention
        fields = ["days"]
        labels = {"days": "Keep conversations for (days)"}
        help_texts = {
            "days": (
                "Conversations untouched for longer than this are deleted automatically. "
                "Enter 0 to keep them indefinitely."
            )
        }

    def clean_days(self):
        days = self.cleaned_data["days"]
        # Ten years is not a policy, just a bound: past it the field is being
        # used to mean "forever", which 0 already says clearly.
        if days > 3650:
            raise forms.ValidationError("Use 0 for indefinite retention rather than a huge number.")
        return days
