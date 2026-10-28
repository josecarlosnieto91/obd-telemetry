/* Portable Console — navegación con mando (D-pad). Compartida por TODAS las páginas.
 *
 * POR QUÉ EXISTE: la pantalla no es táctil y el teclado/ratón no es la vía de uso.
 * El WebView de kiFOSSk (Chromium embebido) NO trae navegación espacial fiable:
 * las flechas no mueven el foco entre tarjetas y no hay indicador de foco visible.
 * Aquí se implementa la navegación explícitamente, sin depender del WebView.
 *
 * TECLAS
 *   Flechas     mover el foco (si no hay nada en esa dirección, se deja pasar el
 *               scroll nativo)
 *   OK / Enter  activar (click)
 *   Escape/Back volver a Inicio de la consola (avisa al launcher)
 *   1..9        atajo de módulo (solo en la consola, si define CONSOLE_SHORTCUTS)
 *   Info/Menu   no se intercepta
 *
 * La página puede personalizar:
 *   window.CONSOLE_ON_BACK   función a ejecutar con Escape/Back
 *   window.CONSOLE_SHORTCUTS { "1": "coches", ... }
 */
(function () {
  'use strict';

  var FOCUSABLE = '[data-focusable],button,a[href],select,input,textarea,[tabindex="0"]';
  var current = null;

  function visible(el) {
    if (!el || el.disabled) return false;
    var r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    var st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
    return true;
  }

  function items() {
    var all = document.querySelectorAll(FOCUSABLE);
    var out = [];
    for (var i = 0; i < all.length; i++) if (visible(all[i])) out.push(all[i]);
    return out;
  }

  function paint(el) {
    if (current && current !== el) current.classList.remove('kp-focus');
    current = el;
    if (el) el.classList.add('kp-focus');
  }

  function focusEl(el) {
    if (!el) return;
    try { el.focus({ preventScroll: true }); } catch (e) { el.focus(); }
    paint(el);
    try {
      el.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
    } catch (e) {
      el.scrollIntoView(false);
    }
  }

  /* Movimiento geométrico: busca el elemento más «en esa dirección», penalizando
     la desalineación. Funciona igual en una rejilla, una barra o una lista. */
  function move(dir) {
    var list = items();
    if (!list.length) return false;
    var cur = list.indexOf(document.activeElement);
    if (cur === -1) { focusEl(list[0]); return true; }

    var cr = list[cur].getBoundingClientRect();
    var cx = cr.left + cr.width / 2, cy = cr.top + cr.height / 2;
    var best = null, bestScore = Infinity;

    for (var i = 0; i < list.length; i++) {
      if (i === cur) continue;
      var r = list[i].getBoundingClientRect();
      var x = r.left + r.width / 2, y = r.top + r.height / 2;
      var dx = x - cx, dy = y - cy;
      var ok = (dir === 'right' && dx > 4) || (dir === 'left' && dx < -4) ||
               (dir === 'down' && dy > 4) || (dir === 'up' && dy < -4);
      if (!ok) continue;
      var primary = (dir === 'left' || dir === 'right') ? Math.abs(dx) : Math.abs(dy);
      var secondary = (dir === 'left' || dir === 'right') ? Math.abs(dy) : Math.abs(dx);
      var score = primary + secondary * 2.5;
      if (score < bestScore) { bestScore = score; best = list[i]; }
    }
    if (best) { focusEl(best); return true; }
    return false;
  }

  function activate() {
    var el = document.activeElement;
    if (!el || el === document.body) return false;
    if (el.tagName === 'SELECT') { el.click(); return true; }
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return true;
    el.click();
    return true;
  }

  function goHome() {
    if (typeof window.CONSOLE_ON_BACK === 'function') { window.CONSOLE_ON_BACK(); return; }
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ t: 'console:home' }, '*');
      }
    } catch (e) { /* sin padre accesible */ }
  }

  /* Pedir a la consola que cambie de módulo. Los módulos viven en iframes de OTRO
     origen y no pueden tocar la barra, así que piden el cambio por nombre y la
     consola lo resuelve con SU tabla de módulos (una sola lista, sin duplicarla).
     `url` es solo la salida de emergencia para cuando la página se abre suelta en
     un navegador, donde no hay consola a la que pedirle nada. */
  window.consolaIr = function (modulo, url) {
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ t: 'console:ir', modulo: String(modulo || '') }, '*');
        return true;
      }
    } catch (e) { /* sin padre accesible */ }
    if (url) window.location.href = url;
    return false;
  };

  /* Filas desplegables: el mando no tiene hover, así que el detalle se abre y se
     cierra con OK. SOLO UNA a la vez: si se apilan abiertas, el contenido se va de
     la pantalla y con el mando hay que recorrerlo entero para volver a ver el resto.
     Se aplica a las filas que traen su detalle justo detrás (hermano `.detalle`). */
  window.consolaAcordeon = function (raiz) {
    var cont = raiz || document;
    var filas = cont.querySelectorAll('.fila[data-focusable]');
    for (var i = 0; i < filas.length; i++) {
      (function (f) {
        var sig = f.nextElementSibling;
        if (!sig || String(sig.className).indexOf('detalle') === -1) return;
        f.addEventListener('click', function () {
          var abrir = !f.classList.contains('abierta');
          var abiertas = cont.querySelectorAll('.fila.abierta');
          for (var j = 0; j < abiertas.length; j++) {
            if (abiertas[j] !== f) abiertas[j].classList.remove('abierta');
          }
          f.classList.toggle('abierta', abrir);
        });
      })(filas[i]);
    }
  };

  /* Dentro de un módulo (iframe) hace falta un «volver» alcanzable con el mando:
     el WebView se queda el botón BACK (verificado en el dispositivo: no vuelve) y
     el iframe es de OTRO origen, así que no puede tocar la barra del launcher.
     Se inyecta un botón flotante y se manda «volver a Inicio» por postMessage. */
  function injectHomeButton() {
    if (window.parent === window) return;              // es el launcher: ya tiene barra
    if (document.getElementById('kp-home')) return;
    var b = document.createElement('button');
    b.id = 'kp-home';
    b.type = 'button';
    b.setAttribute('data-focusable', '');
    b.textContent = '◀ Inicio';
    b.addEventListener('click', function (ev) { ev.stopPropagation(); goHome(); });
    var st = document.createElement('style');
    // El botón va fijo arriba a la izquierda, así que RESERVA ese espacio en el
    // body: sin esto tapaba el título de las páginas (el nombre del lugar del
    // clima salía como «edo» en la TV). Un control flotante no puede pisar contenido.
    st.textContent = '#kp-home{position:fixed;top:8px;left:8px;z-index:2147483646;' +
      'background:#4f8cff;color:#0a0a0a;border:0;border-radius:9px;padding:9px 15px;' +
      'font:600 15px system-ui,sans-serif;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.55);}' +
      'body{padding-top:60px !important;}';
    (document.head || document.documentElement).appendChild(st);
    // Primero en el DOM: así se alcanza con una flecha ARRIBA desde el contenido
    if (document.body) document.body.insertBefore(b, document.body.firstChild);
    else document.documentElement.appendChild(b);
  }

  var ARROWS = { 37: 'left', 38: 'up', 39: 'right', 40: 'down' };
  var OK = { 13: 1, 23: 1, 66: 1, 160: 1 };   // Enter, DPAD_CENTER, ENTER, NUM_ENTER
  var BACK = { 27: 1, 8: 1, 4: 1, 461: 1 };   // Escape, Backspace, BACK, ESC
  // El mando del salón no tiene teclado numérico: sus teclas «extra» son Menú (≡),
  // Ajustes (⚙) y Salir (✕). Si alguna llega a la página, se usa como «volver».
  var EXTRA_BACK = { 82: 1, 176: 1, 111: 1, 3: 1, 84: 1, 125: 1, 127: 1 };

  document.addEventListener('keydown', function (ev) {
    var t = ev.target;
    var typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA');

    if (ARROWS[ev.keyCode]) {
      // Escribiendo en un campo: las flechas son del cursor, no de la navegación.
      if (typing && (ev.keyCode === 37 || ev.keyCode === 39)) return;
      if (move(ARROWS[ev.keyCode])) ev.preventDefault();
      return;
    }
    if (OK[ev.keyCode]) { if (activate()) ev.preventDefault(); return; }
    if (BACK[ev.keyCode] || EXTRA_BACK[ev.keyCode]) { ev.preventDefault(); goHome(); return; }

    if (!typing && ev.keyCode >= 49 && ev.keyCode <= 57) {
      // Atajos de la consola (1..6). El mando del salón NO tiene teclado numérico:
      // esto es solo para teclado/ratón, no es la vía de uso.
      var map = window.CONSOLE_SHORTCUTS;
      var n = String(ev.keyCode - 48);
      if (map && map[n]) { ev.preventDefault(); map[n](); }
    }
  }, true);

  // Si el WebView pierde el foco al cargar, la primera flecha debe funcionar igual:
  document.addEventListener('focusin', function (ev) { paint(ev.target); });

  function init() {
    injectHomeButton();
    if (!current) {
      var list = items();
      // El foco inicial va al CONTENIDO, no al botón «Inicio» inyectado
      var first = (list[0] && list[0].id === 'kp-home' && list.length > 1) ? list[1] : list[0];
      focusEl(first);
    }
  }

  if (document.readyState === 'loading') {
    window.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
  window.addEventListener('load', function () { if (!current) init(); });

  // Estilo ÚNICO de foco para las cinco páginas: sin esto el mando navega a ciegas.
  var st = document.createElement('style');
  st.textContent =
    '.kp-focus{outline:3px solid #4f8cff !important;outline-offset:3px !important;' +
    'box-shadow:0 0 0 5px rgba(79,140,255,.28) !important;border-radius:10px;}' +
    '@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto !important;}}';
  (document.head || document.documentElement).appendChild(st);
})();
