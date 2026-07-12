"""Help center views.

Two audiences, one body of content:

- Staff read role-filtered help inside the dashboard (`help_index`), and
  managers author their venue's articles (`help_manage` + CRUD). Gating
  reuses accounts.permissions exactly like the rest of the dashboard —
  `tenant_staff_required` to read, `manager_required` to author.
- Ticket buyers read the PUBLIC subset on the storefront (`public_faq`),
  gated by `require_tenant` like the other storefront pages so it only
  exists on a real theater subdomain, never the platform host.

Tenant-authored HelpArticles and the shipped `builtins` are merged into the
same category groups so a reader sees one coherent list; only the real
articles carry edit/delete controls (`is_builtin` distinguishes them).
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.permissions import manager_required, tenant_staff_required
from tenants.decorators import require_tenant

from . import builtins
from .forms import HelpArticleForm
from .models import HelpArticle


def _group_by_category(articles, builtin_articles):
    """Merge DB articles and built-ins into ordered category groups.

    Returns a list of {"label": ..., "articles": [...]} in HelpArticle
    category order, dropping empty categories. Within a category the tenant's
    own articles come first (already position-sorted by the caller), then the
    built-in defaults.
    """
    by_category = {}
    for article in articles:
        by_category.setdefault(article.category, []).append(article)
    for article in builtin_articles:
        by_category.setdefault(article.category, []).append(article)

    groups = []
    for value, label in HelpArticle.Category.choices:
        items = by_category.get(value)
        if items:
            groups.append({"label": label, "articles": items})
    return groups


# --- staff: read ----------------------------------------------------------


@tenant_staff_required
def help_index(request):
    membership = request.membership
    articles = list(HelpArticle.objects.readable_by(request.organization, membership))
    groups = _group_by_category(articles, builtins.readable_by(membership))
    return render(
        request,
        "helpcenter/help_index.html",
        {
            "groups": groups,
            "can_manage": membership.can_manage_events(),
        },
    )


# --- staff: author (manager+) --------------------------------------------


@manager_required
def help_manage(request):
    articles = HelpArticle.objects.for_organization(request.organization).order_by(
        "category", "position", "title"
    )
    return render(request, "helpcenter/help_manage.html", {"articles": articles})


@manager_required
def help_create(request):
    if request.method == "POST":
        form = HelpArticleForm(request.POST, organization=request.organization)
        if form.is_valid():
            form.instance.created_by = request.user
            article = form.save()
            messages.success(request, f"Created “{article.title}”.")
            return redirect("dashboard_help_manage")
    else:
        form = HelpArticleForm(organization=request.organization)
    return render(request, "helpcenter/help_form.html", {"form": form})


@manager_required
def help_update(request, pk):
    article = get_object_or_404(
        HelpArticle, pk=pk, organization=request.organization
    )
    if request.method == "POST":
        form = HelpArticleForm(
            request.POST, instance=article, organization=request.organization
        )
        if form.is_valid():
            form.save()
            messages.success(request, f"Updated “{article.title}”.")
            return redirect("dashboard_help_manage")
    else:
        form = HelpArticleForm(instance=article, organization=request.organization)
    return render(
        request, "helpcenter/help_form.html", {"form": form, "object": article}
    )


@manager_required
@require_POST
def help_delete(request, pk):
    article = get_object_or_404(
        HelpArticle, pk=pk, organization=request.organization
    )
    title = article.title
    article.delete()
    messages.success(request, f"Deleted “{title}”.")
    return redirect("dashboard_help_manage")


# --- storefront: buyers ---------------------------------------------------


@require_tenant
def public_faq(request):
    articles = list(HelpArticle.objects.public(request.organization))
    groups = _group_by_category(articles, builtins.public())
    return render(request, "helpcenter/public_faq.html", {"groups": groups})
