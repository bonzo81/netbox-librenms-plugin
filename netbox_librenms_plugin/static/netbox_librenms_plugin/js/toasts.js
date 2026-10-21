// NetBox shows every toast that is not showing after each HTMX swap, so a hidden toast leaves the page.
document.addEventListener('hidden.bs.toast', event => event.target.remove());
