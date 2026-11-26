(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const clean = (text) => text.trim().normalize('NFKC');

  function make(tag, attributes = {}, text = '') {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attributes)) el.setAttribute(key, value);
    el.textContent = text;
    return el;
  }

  function combobox(id, field, label, options, choose, inputChanged) {
    const box = $(id + '-box');
    box.className = 'manual-combo';
    const control = make('div', { class: 'manual-combo-control' });
    const input = make('input', { id, name: field, role: 'combobox', 'aria-autocomplete': 'list',
      'aria-expanded': 'false', 'aria-controls': id + '-options', maxlength: '80', autocomplete: 'off', required: '' });
    const toggle = make('button', { type: 'button', 'aria-label': '展开' + label, 'aria-controls': id + '-options' }, '⌄');
    const list = make('div', { id: id + '-options', class: 'manual-options', role: 'listbox', 'aria-label': label, tabindex: '-1' });
    let index = -1;
    let choices = [];

    const close = () => {
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
    };
    function select(option) {
      close();
      input.value = option.value;
      choose(option);
      input.focus();
    }
    function render(filter = input.value) {
      choices = options(clean(filter));
      index = -1;
      input.removeAttribute('aria-activedescendant');
      let group;
      const rows = [];
      for (const [position, option] of choices.entries()) {
        if (option.group && option.group !== group) {
          group = option.group;
          rows.push(make('div', { class: 'manual-option-group', 'aria-hidden': 'true' }, group));
        }
        const item = make('div', { id: id + '-option-' + position, class: 'manual-option', role: 'option',
          'aria-selected': 'false' }, option.label);
        item.addEventListener('mousedown', (event) => event.preventDefault());
        item.addEventListener('click', () => select(option));
        rows.push(item);
      }
      if (!rows.length) rows.push(make('div', { class: 'manual-option-group' }, '暂无匹配选项'));
      list.replaceChildren(...rows);
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    }
    toggle.addEventListener('click', () => {
      if (!list.hidden) close();
      else {
        input.focus();
        render('');
        inputChanged?.(true);
      }
    });
    input.addEventListener('input', () => {
      inputChanged?.(false);
      render();
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !list.hidden) {
        event.preventDefault();
        close();
      } else if (event.key === 'Enter' && !list.hidden && index >= 0) {
        event.preventDefault();
        select(choices[index]);
      } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (list.hidden) render();
        if (!choices.length) return;
        const previous = $(id + '-option-' + index);
        previous?.setAttribute('aria-selected', 'false');
        index = index < 0 && event.key === 'ArrowUp' ? choices.length - 1 :
          Math.max(0, Math.min(choices.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)));
        const item = $(id + '-option-' + index);
        item.setAttribute('aria-selected', 'true');
        input.setAttribute('aria-activedescendant', item.id);
        item.scrollIntoView({ block: 'nearest' });
      }
    });
    // Native scrollbar clicks may blur the input without supplying a focus target.
    box.addEventListener('focusout', (event) => {
      if (event.relatedTarget && !box.contains(event.relatedTarget)) close();
    });
    document.addEventListener('pointerdown', (event) => {
      if (!list.hidden && !box.contains(event.target)) close();
    });
    list.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && event.target === list) {
        event.preventDefault();
        close();
        input.focus();
      }
    });
    control.append(input, toggle);
    box.append(make('label', { for: id }, label), control, list);
    close();
    return { input, close, refresh(filter) { if (!list.hidden) render(filter); } };
  }

  function roundedGross(units, price) {
    if (!/^\d{1,13}$/.test(units) || !/^\d{1,9}(?:\.\d{1,8})?$/.test(price)) return '';
    const [integer, fraction = ''] = price.split('.');
    const scale = 10n ** BigInt(fraction.length);
    const cents = (BigInt(integer + fraction) * BigInt(units) * 100n + scale / 2n) / scale;
    return `${cents / 100n}.${String(cents % 100n).padStart(2, '0')}`;
  }

  window.initPortfolioManual = ({ request, onSaved }) => {
    const dialog = $('manual-dialog');
    const form = $('manual-entry-form');
    const field = (name) => form.elements.namedItem(name);
    const status = $('manual-status');
    const save = $('manual-save');
    const candidates = $('manual-candidates');
    let kind;
    let info;
    let submission;
    let confirmation;
    let newBroker;
    let securities = [];
    let pendingSearch;
    let searchSequence = 0;
    let saving = false;

    const accountMatches = (text) => info?.accounts.filter(({broker}) =>
      broker === text || broker.replace(/证券$/, '') === text.replace(/证券$/, '')) ?? [];
    const businessValue = () => info?.businesses[kind].find(({label, aliases}) =>
      [label, ...aliases].includes(clean(business.input.value)));
    const clearConfirmation = () => {
      confirmation = '';
      candidates.replaceChildren();
      candidates.hidden = true;
      save.textContent = '保存';
      status.textContent = '';
    };
    function showField(name, visible, required = false) {
      const input = field(name);
      input.disabled = !visible;
      input.required = visible && required;
      input.closest('label').hidden = !visible;
    }
    function updateFields() {
      const current = businessValue();
      const history = kind === 'history';
      const mode = current?.mode;
      const trade = mode === 'trade';
      const ipo = mode === 'ipo';
      $('manual-security-box').hidden = !history;
      security.input.disabled = !history;
      showField('name', history && !ipo, history && !ipo);
      showField('quantity', trade || mode === 'credit' || ipo, !ipo);
      showField('price', trade, true);
      showField('gross_amount', trade || mode === 'cash', mode === 'cash');
      showField('fees', trade);
      showField('net_cash', trade);
      showField('order_reference', history);
      $('manual-date-label').textContent = history ? (trade ? '成交日期' : '业务日期') : '转账日期';
      $('manual-quantity-label').textContent = ipo ? '配号数量' : mode === 'credit' ? '入账数量' : '成交数量';
      $('manual-reference-label').textContent = !history ? '银行流水号' : ipo ? '配号编号' : '成交编号';
      $('manual-amount-label').textContent = trade ? '券商实际成交额（可选）' : '实际金额';
      const gross = trade ? roundedGross(clean(field('quantity').value), clean(field('price').value)) : '';
      const cashSide = ['转出证券', '股息红利税补缴'].includes(current?.label) ? '资金流出' : '资金流入';
      const actual = clean(field('gross_amount').value);
      $('manual-preview').textContent = (gross ? `按数量和价格计算 ${gross} 元${actual ? ` · 采用券商成交额 ${actual} 元` : ''}` :
        mode === 'cash' ? `${cashSide}${actual ? ` ${actual} 元` : ''}` : '');
      if (ipo) $('manual-more').open = true;
    }
    function scheduleSearch(query) {
      const sequence = ++searchSequence;
      clearTimeout(pendingSearch);
      if (kind !== 'history') return;
      pendingSearch = setTimeout(async () => {
        const params = new URLSearchParams({ q: query });
        const matched = accountMatches(clean(broker.input.value));
        if (matched.length === 1) params.set('account', matched[0].id);
        try {
          const result = await request('/api/portfolio/manual-options?' + params);
          if (sequence === searchSequence && dialog.open) {
            securities = result.securities;
            security.refresh(query);
            const exact = securities.find(({code}) => code === clean(security.input.value));
            if (exact && !field('name').value) field('name').value = exact.name;
          }
        } catch (error) {
          if (sequence === searchSequence && dialog.open) status.textContent = error.message;
        }
      }, 120);
    }
    const broker = combobox('manual-broker', 'broker', '券商', (text) => {
      const options = (info?.accounts ?? []).filter(({broker}) => broker.includes(text))
        .map(({broker}) => ({ value: broker, label: broker }));
      if (text && !accountMatches(text).length) options.push({ value: text, label: '新增券商：' + text, isNew: true });
      return options;
    }, (option) => {
      newBroker = option.isNew ? option.value : '';
      clearConfirmation();
      scheduleSearch(clean(security.input.value));
    }, (expanded) => {
      if (expanded) return;
      newBroker = '';
      scheduleSearch(clean(security.input.value));
    });
    const business = combobox('manual-business', 'business', '业务', (text) =>
      (info?.businesses[kind] ?? []).filter(({label, aliases}) => [label, ...aliases].some((word) => word.includes(text)))
        .map(({label, group}) => ({ value: label, label, group })), () => {
      clearConfirmation();
      updateFields();
    });
    const security = combobox('manual-security', 'code', '证券代码', () => securities.map((row) => ({
      ...row, value: row.code, label: `${row.code} ${row.name} · ${row.exchange.toUpperCase()} ${row.asset_type === 'etf' ? 'ETF' : 'A 股'}`
    })), (row) => {
      field('name').value = row.name;
      clearConfirmation();
    }, (expanded) => {
      if (!expanded) {
        field('name').value = '';
        securities = [];
      }
      scheduleSearch(expanded ? '' : clean(security.input.value));
    });

    async function open(mode) {
      kind = mode;
      info = null;
      newBroker = '';
      securities = [];
      submission = crypto.randomUUID();
      const generation = submission;
      form.reset();
      $('manual-more').open = false;
      clearConfirmation();
      for (const combo of [broker, business, security]) combo.close();
      $('manual-title').textContent = kind === 'history' ? '手动添加交易' : '手动添加转账';
      updateFields();
      save.disabled = true;
      status.textContent = '正在读取本地账户与业务选项。';
      dialog.showModal();
      try {
        const accountId = $('account-select').value;
        const query = new URLSearchParams(accountId ? { account: accountId } : {});
        const result = await request('/api/portfolio/manual-options?' + query);
        if (!dialog.open || generation !== submission) return;
        info = result;
        securities = result.securities;
        field('event_date').value = result.today;
        field('event_date').max = result.today;
        const current = result.accounts.find(({id}) => String(id) === accountId);
        broker.input.value = current?.broker ?? (result.accounts.length === 1 ? result.accounts[0].broker : '');
        status.textContent = '';
        save.disabled = false;
        broker.input.focus();
      } catch (error) {
        if (dialog.open && generation === submission) status.textContent = error.message;
      }
    }

    for (const button of document.querySelectorAll('[data-manual-entry]')) {
      button.addEventListener('click', () => open(button.dataset.manualEntry));
    }
    $('manual-close').addEventListener('click', () => dialog.close());
    dialog.addEventListener('cancel', (event) => { if (saving) event.preventDefault(); });
    dialog.addEventListener('close', () => { searchSequence++; });
    form.addEventListener('input', () => { clearConfirmation(); updateFields(); });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!info || saving) return;
      if (!businessValue()) { status.textContent = '请选择有效的业务类别。'; return; }
      const values = Object.fromEntries(new FormData(form));
      if (!accountMatches(clean(values.broker)).length && newBroker !== clean(values.broker)) {
        status.textContent = '请从券商下拉选择要新增的名称。';
        broker.input.focus();
        return;
      }
      saving = true;
      save.disabled = $('manual-close').disabled = true;
      try {
        const endpoint = kind === 'history' ? 'trade' : 'transfer';
        const result = await request('/api/portfolio/manual/' + endpoint,
          { ...values, request_id: submission, new_broker: newBroker, confirmation });
        if (result.confirmation) {
          confirmation = result.confirmation;
          status.textContent = '已找到疑似重复记录。若这是另一笔，请确认另存。';
          candidates.replaceChildren(...result.candidates.map((row) => make('li', {},
            [row.event_date, row.event_time, row.source_label, row.code, row.gross_amount].filter(Boolean).join(' · '))));
          candidates.hidden = false;
          save.textContent = '确认另存';
          return;
        }
        dialog.close();
        onSaved(kind, result);
      } catch (error) {
        status.textContent = error.message;
      } finally {
        saving = false;
        save.disabled = $('manual-close').disabled = false;
      }
    });
  };
})();
