"""Tenant context, roles and permissions.

A TenantContext says who is acting, for which business, with which roles. Only a trusted
resolver that has checked stored membership may create one; request fields never grant access.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from sales_platform.platform.domain.validation import require_text


class Role(StrEnum):
    ADMINISTRATOR = "administrator"
    SALES_OPERATOR = "sales_operator"
    APPROVER = "approver"
    VIEWER = "viewer"
    # For API keys used by systems that only forward enquiries (not assignable to people).
    INTAKE_SERVICE = "intake_service"


class Permission(StrEnum):
    VIEW_QUOTATIONS = "quotations:view"
    DRAFT_QUOTATIONS = "quotations:draft"
    APPROVE_QUOTATIONS = "quotations:approve"
    MANAGE_TENANT = "tenant:manage"
    SUBMIT_ENQUIRIES = "enquiries:submit"
    VIEW_ENQUIRIES = "enquiries:view"
    # Accept or decline an issued quotation (customer systems only, through their API key).
    RESPOND_QUOTATIONS = "quotations:respond"
    # See and (from phase 5) enable or disable the business's tools (connections).
    MANAGE_INTEGRATIONS = "integrations:manage"
    # See colleagues, invite people, change their roles and remove them (phase 2).
    MANAGE_MEMBERS = "members:manage"
    # Read and edit the business's own product list (phase 3, Q14).
    VIEW_CATALOGUE = "catalogue:view"
    MANAGE_CATALOGUE = "catalogue:manage"
    # Customers and conversations (phase 4): the inbox and contacts; full email/phone (viewers
    # see them masked, Q12); merging two contacts (D13).
    VIEW_CONVERSATIONS = "conversations:view"
    VIEW_CONTACT_DETAILS = "contacts:details"
    MERGE_CONTACTS = "contacts:merge"
    # Send a failed customer message again (phase 5, user answer 2026-10-29).
    RESEND_MESSAGES = "messages:resend"
    # Orders (phase 7, Q2): every staff role sees them; administrators, sales operators and
    # approvers create, move, cancel and resolve them.
    VIEW_ORDERS = "orders:view"
    MANAGE_ORDERS = "orders:manage"


# Administrators do not approve by default: approval is a separate, explicitly granted role.
ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = MappingProxyType(
    {
        Role.ADMINISTRATOR: frozenset(
            {
                Permission.VIEW_QUOTATIONS,
                Permission.DRAFT_QUOTATIONS,
                Permission.MANAGE_TENANT,
                Permission.MANAGE_INTEGRATIONS,
                Permission.MANAGE_MEMBERS,
                Permission.SUBMIT_ENQUIRIES,
                Permission.VIEW_ENQUIRIES,
                Permission.VIEW_CATALOGUE,
                Permission.MANAGE_CATALOGUE,
                Permission.VIEW_CONVERSATIONS,
                Permission.VIEW_CONTACT_DETAILS,
                Permission.MERGE_CONTACTS,
                Permission.RESEND_MESSAGES,
                Permission.VIEW_ORDERS,
                Permission.MANAGE_ORDERS,
            }
        ),
        Role.SALES_OPERATOR: frozenset(
            {
                Permission.VIEW_QUOTATIONS,
                Permission.DRAFT_QUOTATIONS,
                Permission.SUBMIT_ENQUIRIES,
                Permission.VIEW_ENQUIRIES,
                Permission.VIEW_CATALOGUE,
                Permission.MANAGE_CATALOGUE,
                Permission.VIEW_CONVERSATIONS,
                Permission.VIEW_CONTACT_DETAILS,
                Permission.MERGE_CONTACTS,
                Permission.RESEND_MESSAGES,
                Permission.VIEW_ORDERS,
                Permission.MANAGE_ORDERS,
            }
        ),
        Role.APPROVER: frozenset(
            {
                Permission.VIEW_QUOTATIONS,
                Permission.APPROVE_QUOTATIONS,
                Permission.VIEW_ENQUIRIES,
                Permission.VIEW_CATALOGUE,
                Permission.VIEW_CONVERSATIONS,
                Permission.VIEW_CONTACT_DETAILS,
                Permission.VIEW_ORDERS,
                Permission.MANAGE_ORDERS,
            }
        ),
        Role.VIEWER: frozenset(
            {
                Permission.VIEW_QUOTATIONS,
                Permission.VIEW_ENQUIRIES,
                Permission.VIEW_CATALOGUE,
                Permission.VIEW_CONVERSATIONS,
                Permission.VIEW_ORDERS,
            }
        ),
        Role.INTAKE_SERVICE: frozenset(
            {
                Permission.SUBMIT_ENQUIRIES,
                Permission.VIEW_ENQUIRIES,
                Permission.RESPOND_QUOTATIONS,
            }
        ),
    }
)


# The roles a person can hold (invitations, role changes). Never intake_service.
PEOPLE_ROLES: frozenset[Role] = frozenset(
    {Role.ADMINISTRATOR, Role.SALES_OPERATOR, Role.APPROVER, Role.VIEWER}
)


class TenantStatus(StrEnum):
    """Where a business is in its life (phase 2, user answers Q11-Q13, 2026-10-27).

    - setting_up: just created. Everything can be tried end to end, but nothing is sent to
      customers through messaging tools (D3: "nothing is sent to customers before Go live").
    - active:     went live. One-way: a live business never goes back to setting_up (Q12).
    - suspended:  stopped by the platform operator (owner-credential script only, Q13).
    """

    SETTING_UP = "setting_up"
    ACTIVE = "active"
    SUSPENDED = "suspended"


def accepts_intake(status: TenantStatus) -> bool:
    """New enquiries (and customer answers) are taken while setting up, for the test step."""
    return status is not TenantStatus.SUSPENDED


def may_contact_customers(status: TenantStatus) -> bool:
    """Messages to customers through messaging tools: only once live."""
    return status is TenantStatus.ACTIVE


def allows_api_pull(status: TenantStatus) -> bool:
    """The business's own system may fetch approved quotations while setting up (Q27)."""
    return status is not TenantStatus.SUSPENDED


def allows_changes(status: TenantStatus) -> bool:
    """Settings, members, invitations and API keys: read-only while suspended."""
    return status is not TenantStatus.SUSPENDED


class AuthorizationError(Exception):
    """Base for access failures. Messages never reveal whether another tenant exists."""


class AccessDenied(AuthorizationError):
    """The subject is not a member of the requested tenant."""


class PermissionDenied(AuthorizationError):
    """The subject is a member but lacks the permission for this action."""


@dataclass(frozen=True)
class TenantContext:
    tenant_id: UUID
    subject: str
    roles: frozenset[Role]

    def __post_init__(self) -> None:
        require_text(self.subject, "subject")
        if not self.roles:
            raise ValueError("A tenant context needs at least one role")

    @property
    def permissions(self) -> frozenset[Permission]:
        return frozenset().union(*(ROLE_PERMISSIONS[role] for role in self.roles))

    def require(self, permission: Permission) -> None:
        if permission not in self.permissions:
            raise PermissionDenied(f"Permission {permission} is required")
