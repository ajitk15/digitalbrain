"""Optional OpenAI synthesis with explicit application credentials and estimated costs."""

from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from digitalbrain.configuration import read_secret

from .llm_agents import completion
from .model_catalog import MODEL_CHOICES
from .models import AI_PURPOSES, AIConfiguration
from .policy import application_for
from .services import audit, record_ai_usage
from .workbench import access


class AIForm(forms.ModelForm):
    model_choice = forms.ChoiceField(choices=MODEL_CHOICES, label="Model", initial="custom")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["model"].required = False
        if self.instance.pk:
            selected = f"{self.instance.provider}:{self.instance.model}"
            known = {value for _, group in MODEL_CHOICES for value, _ in group}
            self.initial["model_choice"] = selected if selected in known else "custom"

    def clean(self):
        values = super().clean()
        choice = values.get("model_choice")
        if choice and choice != "custom":
            values["provider"], values["model"] = choice.split(":", 1)
        if values.get("enabled") and not values.get("model"):
            self.add_error("model", "Select a listed model or enter a custom model ID.")
        model = values.get("model", "")
        if model and (len(model) > 160 or any(c.isspace() for c in model)):
            self.add_error("model", "Use a valid model ID without spaces.")
        if values.get("provider") == "openai" and model.startswith("claude-"):
            self.add_error("model", "Claude models require the Claude provider.")
        if values.get("provider") == "claude" and model and not model.startswith("claude-"):
            self.add_error("model", "Use a Claude model ID with the Claude provider.")
        return values

    class Meta:
        model = AIConfiguration
        fields = ["model_choice", "provider", "model", "input_rate", "output_rate", "enabled"]
        labels = {
            "model": "Custom model ID",
            "provider": "Provider for custom model",
            "input_rate": "Input price (USD / 1 million tokens)",
            "output_rate": "Output price (USD / 1 million tokens)",
        }
        help_texts = {
            "model": "Only needed for Custom. Your provider account must have access to the model.",
            "input_rate": "Use your contracted price. Recorded costs are estimates.",
        }


def invoke_ai(user, app_id, purpose, question, citations, history=None, **options):
    app, grant = application_for(user, app_id)
    access(user, app_id, "knowledge")
    if purpose in {"chat", "graph_retrieval"}:
        access(user, app_id, "chat")
    elif purpose == "graph_generation":
        if grant.role not in {"owner", "contributor"}:
            raise PermissionDenied
    else:
        raise ValidationError("Unsupported AI purpose.")
    config = AIConfiguration.objects.filter(application=app, purpose=purpose, enabled=True).first()
    if not config:
        raise ValidationError(
            f"An application owner must configure {dict(AI_PURPOSES)[purpose]} first."
        )
    if not citations and purpose != "chat":
        return "No matching evidence was found. No AI request was made."
    token = read_secret(settings.SECRET_DIRECTORY, f"{config.provider}_{app.pk}")
    adapter = completion
    if config.provider == "claude":
        from .claude_agents import completion as adapter
    elif config.provider != "openai":
        raise ValidationError("Unsupported AI provider.")
    result, answer = adapter(config, question, citations, token, history=history, **options)
    usage = result["usage"]
    amount = (
        (
            Decimal(usage["prompt_tokens"]) * config.input_rate
            + Decimal(usage["completion_tokens"]) * config.output_rate
        )
        / Decimal(1000000)
    ).quantize(Decimal("0.00000001"))
    record_ai_usage(
        actor=user,
        application_id=app.pk,
        provider="OpenAI" if config.provider == "openai" else "Anthropic",
        model=config.model,
        request_id=result["id"],
        purpose=purpose,
        input_tokens=usage["prompt_tokens"],
        output_tokens=usage["completion_tokens"],
        amount=amount,
        estimated=True,
    )
    access(user, app_id, "knowledge")
    if purpose in {"chat", "graph_retrieval"}:
        access(user, app_id, "chat")
    if purpose == "graph_generation":
        access(user, app_id, "knowledge", write=True)
    return answer


def answer_with_ai(user, app_id, question, citations, history=None):
    return invoke_ai(user, app_id, "chat", question, citations, history=history)


@login_required
@require_http_methods(["GET", "POST"])
def ai_settings(request, pk):
    app, grant = application_for(request.user, pk)
    if grant.role != "owner":
        raise PermissionDenied
    selected_purpose = request.POST.get("purpose")
    if request.method == "POST" and selected_purpose not in dict(AI_PURPOSES):
        raise PermissionDenied("Unknown AI purpose.")
    sections = []
    for purpose, label in AI_PURPOSES:
        config = AIConfiguration.objects.filter(application=app, purpose=purpose).first()
        submitted = request.method == "POST" and selected_purpose == purpose
        form = AIForm(
            request.POST if submitted else None,
            instance=config,
            prefix=purpose,
            initial={"enabled": False, "input_rate": 0, "output_rate": 0, "provider": "openai"}
            if config is None
            else None,
        )
        if submitted and form.is_valid():
            with transaction.atomic():
                config = form.save(commit=False)
                config.application = app
                config.purpose = purpose
                config.configured_by = request.user
                config.save()
                audit(
                    request.user,
                    "ai.configured",
                    app.pk,
                    app.product.portfolio.organization,
                    details={
                        "purpose": purpose,
                        "provider": config.provider,
                        "model": config.model,
                    },
                )
            messages.success(request, f"{label} settings saved.")
            return redirect("ai-settings", pk=pk)
        sections.append({"purpose": purpose, "label": label, "form": form})
    return render(
        request,
        "ai_settings.html",
        {
            "application": app,
            "sections": sections,
            "openai_secret": f"openai_{app.pk}",
            "claude_secret": f"claude_{app.pk}",
        },
    )
