def module_refusal(context):
    if (error := context.get("inventory_rule_error")) is not None:
        # ruleid: rule-error-message-needs-projection
        return {"message": str(error)}


def public_module_refusal(context):
    if (error := context.get("inventory_rule_error")) is not None:
        # ok: rule-error-message-needs-projection
        return {"message": error.public_message}


class RuleConfigurationErrorMiddleware:
    def process_exception(self, request, exception):
        # ruleid: rule-error-message-needs-projection
        return answer(request, str(exception))


class RuleConfigurationErrorMiddleware:
    def process_exception(self, request, exception):
        # ok: rule-error-message-needs-projection
        return answer(request, exception.public_message)


def ordinary_value(value):
    # ok: rule-error-message-needs-projection
    return str(value)
