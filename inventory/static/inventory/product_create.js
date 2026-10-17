(() => {
    const form = document.querySelector('[data-variant-form]');
    if (!form) return;
    const body = form.querySelector('[data-variant-rows]');
    const template = document.querySelector('#variant-row-template');
    const checkboxes = [...form.querySelectorAll('input[name="selected_sizes"]')];
    const rows = new Map([...body.querySelectorAll('[data-variant-row]')].map(row => [row.dataset.size, row]));

    function updateSummary() {
        const quantities = [...body.querySelectorAll('input[name$="-initial_stock"]')];
        form.querySelector('[data-variant-count]').textContent = body.children.length;
        form.querySelector('[data-variant-quantity]').textContent = quantities.reduce(
            (total, input) => total + Math.max(0, Number(input.value) || 0), 0,
        );
        form.querySelector('[data-variant-empty]').hidden = body.children.length > 0;
    }

    function syncRows() {
        body.replaceChildren();
        let index = 0;
        for (const checkbox of checkboxes.filter(input => input.checked)) {
            let row = rows.get(checkbox.value);
            if (!row) {
                row = template.content.firstElementChild.cloneNode(true);
                rows.set(checkbox.value, row);
            }
            const sizeLabel = checkbox.closest('label').querySelector('span').textContent;
            row.dataset.size = checkbox.value;
            row.querySelector('[data-size-label]').textContent = sizeLabel;
            row.querySelector('input[name$="-size"]').value = checkbox.value;
            for (const element of row.querySelectorAll('[name], [id], [for]')) {
                for (const attribute of ['name', 'id', 'for']) {
                    if (element.hasAttribute(attribute)) {
                        element.setAttribute(attribute, element.getAttribute(attribute).replace(
                            /variants-(?:\d+|__prefix__)-/g, `variants-${index}-`,
                        ));
                    }
                }
            }
            row.querySelector('input[name$="-barcode"]').required = true;
            for (const label of row.querySelectorAll('label')) {
                const fieldName = label.htmlFor.endsWith('-barcode') ? '商品条码' : '初始库存';
                label.textContent = `${sizeLabel} ${fieldName}`;
            }
            body.append(row);
            index += 1;
        }
        form.querySelector('[name="variants-TOTAL_FORMS"]').value = index;
        checkboxes[0]?.setCustomValidity(index ? '' : '请至少选择一个尺码');
        updateSummary();
    }

    checkboxes.forEach(input => input.addEventListener('change', syncRows));
    body.addEventListener('input', updateSummary);
    // 扫码枪以回车结束时移动到下一格，避免直接提交整张表单。
    body.addEventListener('keydown', event => {
        if (event.key !== 'Enter' || !event.target.matches('input[name$="-barcode"]')) return;
        event.preventDefault();
        const currentRow = event.target.closest('tr');
        const nextInput = currentRow.nextElementSibling?.querySelector('input[name$="-barcode"]')
            || currentRow.querySelector('input[name$="-initial_stock"]');
        nextInput?.focus();
    });
    syncRows();

    const imageInput = form.querySelector('#id_image');
    let imageUrl;
    imageInput?.addEventListener('change', () => {
        if (imageUrl) URL.revokeObjectURL(imageUrl);
        const file = imageInput.files?.[0];
        const preview = form.querySelector('.image-preview');
        form.querySelector('[data-file-name]').textContent = file?.name || '未选择图片';
        preview.replaceChildren();
        if (file) {
            const image = document.createElement('img');
            image.alt = file.name;
            image.src = imageUrl = URL.createObjectURL(file);
            preview.append(image);
        }
    });
    form.querySelector('#product-form-errors')?.focus();
})();
