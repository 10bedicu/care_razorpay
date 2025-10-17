from rest_framework.permissions import BasePermission


class IsSuperUserOrReadOnly(BasePermission):
    """
    Custom permission to only allow superusers to create/update razorpay accounts.
    Read permissions are allowed for authenticated users.
    """

    def has_permission(self, request, view):
        # Read permissions for authenticated users
        if request.method in ["GET", "HEAD", "OPTIONS"]:
            return request.user and request.user.is_authenticated

        # Write permissions only for superusers
        return (
            request.user and request.user.is_authenticated and request.user.is_superuser
        )
