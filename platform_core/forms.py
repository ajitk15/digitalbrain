import io
import warnings

from django import forms
from PIL import Image, UnidentifiedImageError

from .models import ApplicationGrant, User


class ResourceForm(forms.Form):
    name = forms.CharField(max_length=120, label="Name")


class OrganizationForm(ResourceForm):
    administrator = forms.ModelChoiceField(queryset=User.objects.filter(is_active=True))


class MemberForm(forms.Form):
    username = forms.CharField(max_length=150, label="Existing user ID")


class ApplicationForm(ResourceForm):
    owner = forms.ModelChoiceField(queryset=User.objects.none(), label="Application owner")
    owner_can_approve = forms.BooleanField(
        required=False, label="Explicitly grant this owner Code Factory approval rights"
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["owner"].queryset = User.objects.filter(
            is_active=True, organizationmember__organization=organization
        )


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
