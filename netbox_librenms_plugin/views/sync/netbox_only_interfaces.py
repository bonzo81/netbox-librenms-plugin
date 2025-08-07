"""
View for handling paginated NetBox-only interfaces in modal.
"""
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string

from dcim.models import Device
from virtualization.models import VirtualMachine
from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView


class NetBoxOnlyInterfacesView(BaseInterfaceTableView):
    """
    View to return paginated NetBox-only interfaces for modal display.
    """

    def get_interfaces(self, obj):
        """Get interfaces related to the object."""
        return obj.interfaces.all()

    def get_redirect_url(self, obj):
        """Get the redirect URL for the object - not used in this view."""
        return ""

    def get_table(self, data, obj, interface_name_field):
        """Get table instance - not used in this view since we return JSON."""
        return None

    def get(self, request, model_type, pk):
        """
        Return paginated NetBox-only interfaces as JSON for AJAX requests.
        """
        # Set the request attribute for the view
        self.request = request
        
        # Determine the model based on model_type
        if model_type == 'device':
            self.model = Device
        elif model_type == 'vm':
            self.model = VirtualMachine
        else:
            return JsonResponse({'error': 'Invalid model type'}, status=400)

        try:
            obj = get_object_or_404(self.model, pk=pk)
        except Exception as e:
            return JsonResponse({'error': f'Object not found: {str(e)}'}, status=404)
        
        # Get interface name field
        interface_name_field = request.GET.get('interface_name_field', 'ifName')
        
        try:
            # Get context data (reuse existing logic)
            context_data = self.get_context_data(request, obj, interface_name_field)
            netbox_only_interfaces = context_data.get('netbox_only_interfaces', [])
        except Exception as e:
            import traceback
            return JsonResponse({
                'error': f'Error fetching interfaces: {str(e)}',
                'traceback': traceback.format_exc()
            }, status=500)
        
        # Pagination parameters
        page_number = request.GET.get('page', 1)
        per_page = int(request.GET.get('per_page', 25))  # NetBox default
        
        # Create paginator
        paginator = Paginator(netbox_only_interfaces, per_page)
        page_obj = paginator.get_page(page_number)
        
        # Generate smart page range (similar to NetBox's pagination)
        page_range = []
        if paginator.num_pages <= 10:
            # Show all pages if 10 or fewer
            page_range = list(range(1, paginator.num_pages + 1))
        else:
            # Smart pagination - show pages around current page
            if page_obj.number <= 5:
                page_range = list(range(1, 8)) + ['...', paginator.num_pages]
            elif page_obj.number > paginator.num_pages - 5:
                page_range = [1, '...'] + list(range(paginator.num_pages - 6, paginator.num_pages + 1))
            else:
                page_range = [1, '...'] + list(range(page_obj.number - 2, page_obj.number + 3)) + ['...', paginator.num_pages]
        
        # Prepare pagination info
        pagination_info = {
            'current_page': page_obj.number,
            'total_pages': paginator.num_pages,
            'total_count': paginator.count,
            'has_previous': page_obj.has_previous(),
            'has_next': page_obj.has_next(),
            'previous_page_number': page_obj.previous_page_number() if page_obj.has_previous() else None,
            'next_page_number': page_obj.next_page_number() if page_obj.has_next() else None,
            'start_index': page_obj.start_index(),
            'end_index': page_obj.end_index(),
            'per_page': per_page,
            'page_range': page_range,
        }
        
        try:
            # Render the table content
            table_html = render_to_string(
                'netbox_librenms_plugin/modals/_netbox_only_interfaces_table.html',
                {
                    'interfaces': page_obj.object_list,
                    'pagination': pagination_info,
                    'model_type': model_type,
                    'object_pk': pk,
                    'interface_name_field': interface_name_field,
                },
                request=request
            )
        except Exception as e:
            import traceback
            return JsonResponse({
                'error': f'Error rendering template: {str(e)}',
                'traceback': traceback.format_exc()
            }, status=500)
        
        return JsonResponse({
            'table_html': table_html,
            'pagination': pagination_info,
        })
