"""Help-article authoring form. Like the dashboard's other ModelForms it
takes the acting `organization` explicitly and sets it itself, so a POST can
never point a save at another tenant's data (see docs/ARCHITECTURE.md
"Tenant isolation is non-negotiable")."""

from django import forms
from django.utils.text import slugify

from .models import HelpArticle


class HelpArticleForm(forms.ModelForm):
    class Meta:
        model = HelpArticle
        fields = [
            "title",
            "slug",
            "summary",
            "category",
            "visibility",
            "body",
            "position",
            "is_published",
        ]
        widgets = {
            "summary": forms.Textarea(attrs={"rows": 2}),
            "body": forms.Textarea(attrs={"rows": 14}),
        }
        help_texts = {
            "slug": "Optional — leave blank to generate one from the title.",
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.fields["slug"].required = False

    def clean_slug(self):
        # `organization` isn't a form field, so Django's own unique_together
        # validation for (organization, slug) is skipped -- do the per-tenant
        # uniqueness check by hand so a clash surfaces as a field error rather
        # than an IntegrityError at save() (same pattern as SectionForm). A
        # blank slug is fine here: the model derives a unique one on save.
        slug = self.cleaned_data.get("slug")
        if not slug:
            return slug
        slug = slugify(slug)
        clash = HelpArticle.objects.filter(organization=self.organization, slug=slug)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("An article with this slug already exists.")
        return slug

    def save(self, commit=True):
        article = super().save(commit=False)
        article.organization = self.organization
        if commit:
            article.save()
        return article
