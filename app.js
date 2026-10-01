(function(){
  'use strict';
  var COIN = 1e6, CAP = 50e6 * COIN, HALVING = 210000;

  function $(id){ return document.getElementById(id); }
  function fmtD(u){ return (u / COIN).toLocaleString('en-US', {maximumFractionDigits: 2}) + ' DCC'; }
  function fmtN(n){ return Number(n).toLocaleString('en-US'); }
  function shortH(h){ return String(h).slice(0, 14) + '…'; }
  function timeStr(ts){ return new Date(ts * 1000).toLocaleTimeString([], {hour12: false}); }
  function set(id, text){ var el = $(id); if (el) el.textContent = text; }

  function jget(p){
    return fetch(p).then(function(r){
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  function badge(on){
    var b = $('net-badge');
    if (!b) return;
    b.classList.toggle('off', !on);
    set('net-label', on ? 'network online' : 'node unreachable');
  }

  function render(info, chain, mem){
    set('s-height', fmtN(info.height));
    set('s-emission', fmtD(info.emission));
    set('s-reward', fmtD(info.next_subsidy));
    set('s-mempool', fmtN(info.mempool));
    set('v-height', fmtN(info.height));
    set('v-diff', fmtN(info.difficulty));
    set('v-outputs', fmtN(info.outputs));
    set('v-images', fmtN(info.spent_images));
    set('v-mempool', fmtN(info.mempool));

    var epoch = Math.floor((info.height + 1) / HALVING);
    var remain = (epoch + 1) * HALVING - (info.height + 1);
    set('v-halving', fmtN(remain));

    set('e-num', fmtD(info.emission) + '  (' + (info.emission / CAP * 100).toFixed(4) + '%)');
    var fill = $('e-fill');
    if (fill) fill.style.width = Math.max(0.5, Math.min(100, info.emission / CAP * 100)) + '%';

    var rows = (chain.blocks || []).slice(-10).reverse().map(function(b){
      return '<tr>'
        + '<td class="mono">' + b.height + '</td>'
        + '<td class="mono">' + shortH(b.hash) + '</td>'
        + '<td>' + b.txs + '</td>'
        + '<td class="mono">' + fmtN(b.difficulty) + '</td>'
        + '<td class="mono">' + fmtD(b.reward) + '</td>'
        + '<td class="mono">' + timeStr(b.timestamp) + '</td>'
        + '</tr>';
    });
    var body = $('blocks-body');
    if (body) body.innerHTML = rows.join('') ||
      '<tr><td colspan="6" class="muted">no blocks yet</td></tr>';
  }

  function refresh(){
    Promise.all([jget('/api/info'), jget('/api/chain'), jget('/api/mempool')])
      .then(function(res){ render(res[0], res[1], res[2]); badge(true); })
      .catch(function(){ badge(false); });
  }

  setInterval(refresh, 5000);
  refresh();
  // speed race: animate the bars when the section scrolls into view
  var race = $('race');
  if (race && 'IntersectionObserver' in window){
    var fills = race.querySelectorAll('.race-fill');
    var io = new IntersectionObserver(function(entries){
      entries.forEach(function(e){
        if (!e.isIntersecting) return;
        fills.forEach(function(f){
          f.style.setProperty('--w', (f.getAttribute('data-w') || 20) + '%');
        });
        race.classList.add('on');
        io.disconnect();
      });
    }, {threshold: 0.35});
    io.observe(race);
  } else if (race){
    race.classList.add('on');
    race.querySelectorAll('.race-fill').forEach(function(f){
      f.style.setProperty('--w', (f.getAttribute('data-w') || 20) + '%');
    });
  }
})();
