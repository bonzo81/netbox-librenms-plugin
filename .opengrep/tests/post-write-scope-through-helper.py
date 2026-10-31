def create_then_check(self, Cable, cable, user):
    cable.save()
    # ruleid: post-write-scope-through-helper
    if not self.restricted_queryset(Cable, "add").filter(pk=cable.pk).exists():
        raise PermissionError


def create_in_savepoint(Platform, actor, transaction, **fields):
    with transaction.atomic():
        platform = Platform(**fields)
        platform.save()
        # ruleid: post-write-scope-through-helper
        if not Platform.objects.restrict(actor, "add").filter(pk=platform.pk).exists():
            raise PermissionError


def created_by_manager(Tag, user, name):
    tag = Tag.objects.create(name=name)
    # ruleid: post-write-scope-through-helper
    if not Tag.objects.restrict(user, "change").filter(pk=tag.pk).exists():
        raise PermissionError


def save_in_savepoint(self, Interface, interface, transaction, IntegrityError):
    try:
        with transaction.atomic():
            interface.save()
    except IntegrityError as exc:
        raise ValueError from exc
    # ruleid: post-write-scope-through-helper
    if not Interface.objects.restrict(self.request.user, "add").filter(pk=interface.pk).exists():
        raise PermissionError


def create_in_try(Tag, user, name, IntegrityError):
    try:
        tag = Tag.objects.create(name=name)
    except IntegrityError:
        raise
    # ruleid: post-write-scope-through-helper
    if user is not None and not Tag.objects.restrict(user, "add").filter(pk=tag.pk).exists():
        raise PermissionError


def check_before_write(Tag, user, tag):
    # ok: post-write-scope-through-helper
    if not Tag.objects.restrict(user, "change").filter(pk=tag.pk).exists():
        raise PermissionError
    tag.save()


def view_check_after_write(self, Interface, interface):
    interface.save()
    # ok: post-write-scope-through-helper
    return self.restricted_queryset(Interface).filter(pk=interface.pk).exists()


def other_row_after_write(self, Cable, cable, other):
    cable.save()
    # ok: post-write-scope-through-helper
    return self.restricted_queryset(Cable, "change").filter(pk=other.pk).exists()


def through_helper(saved_in_scope, cable, user):
    cable.save()
    # ok: post-write-scope-through-helper
    return saved_in_scope(cable, user, "add")
