(function searchMetadata(targetAppId) {
  'use strict';
  // Reads already-rendered public search cards. Never clicks or calls a route.
  const result = {status: 'blocked', target_appid: targetAppId, page_supported: false, cards: [], limits: []};
  function stop(reason) { result.reason = reason; return result; }
  if (!/^wx[0-9a-f]{16}$/.test(targetAppId)) return stop('invalid_appid');
  const text = document.body.innerText || '';
  // Keep explicit throttle markers aligned with the Appium/native OCR guards.
  if (/too (?:many|frequent) (?:requests|attempts)|try again later|操作(?:过于|太)?频繁|访问(?:过于|太)?频繁|请求过于频繁|稍后再试/i.test(text)) {
    result.wall_kind = 'rate_limit';
    const wait = text.match(/(\d+)\s*(seconds?|minutes?|秒|分钟)/i);
    result.retry_after = wait ? Number(wait[1]) * (/minute|分钟/i.test(wait[2]) ? 60 : 1) : 0;
    return stop('public_ui_wall');
  }
  if (/验证身份|安全验证|请先登录|log in to continue/i.test(text)) {
    result.wall_kind = /请先登录|log in to continue/i.test(text) ? 'login' : 'security';
    return stop('public_ui_wall');
  }
  const root = document.querySelector('#app');
  if (!root || !root.__vue__) return stop('public_search_vue_root_unavailable');
  if (!root.__vue__.$route || root.__vue__.$route.name !== 'result') return stop('not_public_result_route');
  result.page_supported = /暂无相关结果|未找到相关结果|没有找到相关|no results found/i.test(document.body.innerText || '');
  const viewport = window.visualViewport;
  result.viewport = {width: innerWidth, height: innerHeight,
    scale: viewport ? viewport.scale : 1, offset_left: viewport ? viewport.offsetLeft : 0,
    offset_top: viewport ? viewport.offsetTop : 0};
  if (result.viewport.scale !== 1 || result.viewport.offset_left !== 0 || result.viewport.offset_top !== 0)
    return stop('zoomed_or_offset_viewport');
  const seen = new Set(), buttons = new Set();
  function handlerText(handler) {
    if (!handler) return '';
    if (Array.isArray(handler)) return handler.map(handlerText).join('\n');
    return String(handler) + (handler.fns ? '\n' + handlerText(handler.fns) : '');
  }
  function binding(node, element, inherited, depth) {
    if (!node || depth > 20) return null;
    const own = node.data && node.data.on && node.data.on.click;
    const active = own ? handlerText(own) : inherited;
    if (node.elm === element) return active;
    const children = node.children || [];
    for (let i = 0; i < children.length; i++) {
      const found = binding(children[i], element, active, depth + 1);
      if (found !== null) return found;
    }
    return null;
  }
  function visit(component, path, depth) {
    if (!component || seen.has(component)) return;
    if (seen.size >= 1200 || depth > 30 || result.cards.length >= 64) {
      result.limits.push('component_scan_bound'); return;
    }
    seen.add(component);
    const item = component.$props && component.$props.item;
    const info = item && item.jumpInfo, element = component.$el;
    if (info && element && element.matches && element.matches('.unified-account')) {
      result.page_supported = true;
      const appid = info.appID === undefined ? info.appId : info.appID;
      const conflict = info.appID !== undefined && info.appId !== undefined && info.appID !== info.appId;
      const candidates = element.querySelectorAll('.account-header .header-detail[role="button"]');
      for (let i = 0; i < candidates.length && result.cards.length < 64; i++) {
        const button = candidates[i];
        if (buttons.has(button)) continue;
        buttons.add(button);
        const r = button.getBoundingClientRect(), style = getComputedStyle(button);
        const rect = {left: r.left, top: r.top, right: r.right, bottom: r.bottom};
        const cx = (r.left + r.right) / 2, cy = (r.top + r.bottom) / 2;
        const hit = document.elementFromPoint(cx, cy);
        const visible = !!button.isConnected && r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0
          && r.right <= innerWidth && r.bottom <= innerHeight && style.display !== 'none'
          && style.visibility !== 'hidden' && style.opacity !== '0' && style.pointerEvents !== 'none'
          && (hit === button || button.contains(hit));
        const methods = component.$options && component.$options.methods || {};
        const source = binding(component._vnode, button, '', 0) || '';
        const bound = source.includes('onTapAccount') && String(methods.onTapAccount).includes('handleWeApp')
          && String(methods.handleWeApp).includes('M_go');
        const validID = typeof appid === 'string' && /^wx[0-9a-f]{16}$/.test(appid) && !conflict;
        const validVersion = Number.isSafeInteger(info.appVersion) && info.appVersion >= 0;
        const clean = value => typeof value === 'string' ? value.replace(/<[^>]*>/g, '').trim().slice(0, 256) : '';
        // Public card schema observed in archived routes.json exposes item.title
        // as a string. Header innerText also includes badges and the publisher;
        // only its first nonempty visible line is a fallback when title is absent.
        const publicTitle = clean(item.title);
        const headerLines = typeof button.innerText === 'string' ? button.innerText.split(/\r?\n/) : [];
        const title = publicTitle || headerLines.map(clean).find(line => line) || '';
        result.cards.push({appid: validID ? appid : null, app_version: validVersion ? info.appVersion : null,
          app_id_aliases: {appID: typeof info.appID === 'string' ? info.appID.slice(0, 64) : null,
                          appId: typeof info.appId === 'string' ? info.appId.slice(0, 64) : null},
          title: title, title_source: publicTitle ? 'item.title' : 'header_first_visible_line',
          developer: clean(item.source && item.source.title),
          component_path: path, selector: '.account-header .header-detail[role="button"]',
          rect_css: rect, visible: visible, bound_to_open_weapp: bound,
          jump_type: info.jumpType, identity_conflict: conflict,
          conflicts_with_target: conflict && (info.appID === targetAppId || info.appId === targetAppId),
          selectable: validID && validVersion && info.jumpType === 2 && visible && bound});
      }
    }
    (component.$children || []).forEach((child, index) => visit(child, path + '.' + index, depth + 1));
  }
  visit(root.__vue__, 'root', 0);
  result.components_inspected = seen.size;
  if (result.limits.length) return stop('incomplete_component_scan');
  if (result.cards.some(card => card.conflicts_with_target)) return stop('target_identity_conflict');
  const matches = result.cards.filter(card => card.appid === targetAppId && card.selectable);
  if (!matches.length && result.cards.some(card => card.appid === targetAppId && card.visible))
    return stop('unverified_card_binding');
  if (matches.length !== 1) return stop(matches.length ? 'ambiguous_target_cards' : 'no_exact_selectable_card');
  result.status = 'ready';
  result.selected = matches[0];
  return result;
})
