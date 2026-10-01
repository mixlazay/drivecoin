(function(){
  'use strict';
  var COIN = 1e6, CAP = 50e6 * COIN, HALVING = 210000;

  function $(id){ return document.getElementById(id); }
  function esc(s){ return String(s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function fmtD(u){ return (u / COIN).toLocaleString('en-US', {maximumFractionDigits: 2}); }
  function fmtN(n){ return Number(n).toLocaleString('en-US'); }
  function short(h, n){ h = String(h); n = n || 10; return h.slice(0, n) + '…' + h.slice(-6); }
  function timeStr(ts){ return new Date(ts * 1000).toLocaleString([], {hour12: false}); }
  function set(id, t){ var el = $(id); if (el) el.textContent = t; }

  function jget(p){
    return fetch(p).then(function(r){
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  // ── stats bar (always) ────────────────────────────────────────────────
  function refreshStats(){
    jget('/api/info').then(function(i){
      set('s-height', fmtN(i.height));
      set('s-diff', fmtN(i.difficulty));
      set('s-emission', fmtD(i.emission) + ' DCC');
      set('s-reward', fmtD(i.next_subsidy) + ' DCC');
      set('s-mempool', fmtN(i.mempool));
      var epoch = Math.floor((i.height + 1) / HALVING);
      set('s-halving', fmtN((epoch + 1) * HALVING - (i.height + 1)));
      $('net-label').textContent = 'network online — auto-refresh every 5s';
      $('net-label').parentNode.classList.remove('off');
    }).catch(function(){
      $('net-label').textContent = 'node unreachable';
      $('net-label').parentNode.classList.add('off');
    });
  }

  // ── helpers ───────────────────────────────────────────────────────────
  function txBadge(t){
    if (t.coinbase) return '<span class="badge coinbase">COINBASE</span>';
    var ch = t.channel;
    if (ch && ch.type === 'open') return '<span class="badge ln-open">LN OPEN</span>';
    if (ch && ch.type === 'close') return '<span class="badge ln-close">LN CLOSE</span>';
    return '<span class="badge transfer">TRANSFER</span>';
  }

  function txCard(t, height){
    var inHtml = (t.inputs || []).map(function(i){
      return '<div class="item"><span class="lbl">ring</span> <span class="val">' + i.ring_size +
        ' keys</span><br><span class="lbl">key image</span> <span class="val">' + esc(i.key_image) + '</span></div>';
    }).join('') || '<div class="item"><span class="lbl">no inputs (coinbase)</span></div>';
    var outHtml = (t.outputs || []).map(function(o){
      return '<div class="item"><span class="lbl">stealth out</span> <span class="val">' + esc(o.dest) +
        '</span><br><span class="lbl">commitment</span> <span class="val">' + esc(o.commitment) + '</span> ' +
        (o.range_proof ? '<span class="badge ok">range ✓</span>' : '') + '</div>';
    }).join('');
    var chHtml = '';
    if (t.channel){
      chHtml = '<div class="txfoot">' + esc(JSON.stringify(t.channel)).slice(0, 300) + '</div>';
    }
    var fee = t.coinbase
      ? 'mints ' + fmtD(t.coinbase_amount) + ' DCC'
      : 'fee ' + fmtD(t.fee) + ' DCC (amounts hidden)';
    return '<div class="txcard">'
      + '<div class="txhead">' + txBadge(t)
      + '<a class="txid" href="#/tx/' + t.txid + '">' + esc(t.txid) + '</a>'
      + (height >= 0 ? '' : ' <span class="badge mem">IN MEMPOOL</span>')
      + '</div>'
      + '<div class="txfoot">' + fee + ' · ' + (t.inputs || []).length + ' input(s) · '
      + (t.outputs || []).length + ' output(s)</div>'
      + '<div class="io"><div><h4>inputs (rings)</h4>' + inHtml + '</div>'
      + '<div><h4>outputs (one-time addresses)</h4>' + outHtml + '</div></div>'
      + chHtml + '</div>';
  }

  // ── views ─────────────────────────────────────────────────────────────
  function viewHome(){
    var v = $('view');
    v.innerHTML = '<h1 class="page">Recent blocks</h1>'
      + '<p class="subline">newest first · click a row for block detail</p>'
      + '<div id="membox"></div><div class="tablewrap"><table>'
      + '<thead><tr><th>height</th><th>hash</th><th>txs</th><th>difficulty</th>'
      + '<th>reward</th><th>time</th></tr></thead>'
      + '<tbody id="blocks-body"><tr><td colspan="6" class="muted">loading…</td></tr></tbody>'
      + '</table></div>';
    Promise.all([jget('/api/chain'), jget('/api/mempool')]).then(function(res){
      var chain = res[0], mem = res[1];
      var rows = (chain.blocks || []).slice().reverse().map(function(b){
        return '<tr class="rowlink" onclick="location.hash=\'#/block/' + b.height + '\'"\>'
          + '<td><a href="#/block/' + b.height + '">' + b.height + '</a></td>'
          + '<td title="' + b.hash + '">' + short(b.hash, 14) + '</td>'
          + '<td>' + b.txs + '</td>'
          + '<td>' + fmtN(b.difficulty) + '</td>'
          + '<td>' + fmtD(b.reward) + '</td>'
          + '<td>' + timeStr(b.timestamp) + '</td></tr>';
      }).join('');
      $('blocks-body').innerHTML = rows ||
        '<tr><td colspan="6" class="muted">no blocks</td></tr>';
      var membox = $('membox');
      if (mem && mem.length){
        membox.innerHTML = '<div class="sect"><h2>Mempool</h2>'
          + '<span class="count">' + mem.length + ' pending</span></div>'
          + mem.map(function(m){
              return '<div class="txcard"><div class="txhead">'
                + '<span class="badge transfer">TRANSFER</span>'
                + '<a class="txid" href="#/tx/' + m.txid + '">' + m.txid + '</a>'
                + ' <span class="badge mem">IN MEMPOOL</span></div>'
                + '<div class="txfoot">fee ' + fmtD(m.fee) + ' DCC · '
                + m.inputs + ' input(s) · rings ' + JSON.stringify(m.rings) + '</div></div>';
            }).join('');
      }
    }).catch(function(e){
      v.innerHTML = '<div class="error">failed to load chain: ' + esc(e.message) + '</div>';
    });
  }

  function viewBlock(h){
    var v = $('view');
    v.innerHTML = '<h1 class="page">Block #' + h + '</h1><p class="subline">loading…</p>';
    Promise.all([jget('/api/block/' + h), jget('/api/chain')]).then(function(res){
      var d = res[0];
      var summary = (res[1].blocks || []).filter(function(b){ return b.height === d.height; })[0];
      var hash = summary ? summary.hash : '(unknown)';
      v.innerHTML = '<h1 class="page">Block #' + d.height + '</h1>'
        + '<p class="subline">mined ' + timeStr(d.timestamp) + '</p>'
        + '<div class="card"><h3>header</h3><div class="kv">'
        + '<div class="k">hash</div><div class="v">' + esc(hash) + '</div>'
        + '<div class="k">previous</div><div class="v">'
        + (d.height > 0 ? '<a href="#/block/' + (d.height - 1) + '">' + esc(d.prev_hash) + '</a>' : 'genesis')
        + (d.height > 0 ? ' <a href="#/block/' + (d.height - 1) + '">(#' + (d.height - 1) + ')</a>' : '')
        + '</div>'
        + '<div class="k">merkle root</div><div class="v">' + esc(d.merkle) + '</div>'
        + '<div class="k">timestamp</div><div class="v">' + d.timestamp + ' (' + timeStr(d.timestamp) + ')</div>'
        + '<div class="k">difficulty</div><div class="v">' + fmtN(d.difficulty) + '</div>'
        + '<div class="k">nonce</div><div class="v">' + fmtN(d.nonce) + '</div>'
        + '<div class="k">transactions</div><div class="v">' + d.txs.length + '</div>'
        + '</div></div>'
        + '<div class="sect"><h2>Transactions</h2><span class="count">' + d.txs.length + '</span></div>'
        + d.txs.map(function(t){ return txCard(t, d.height); }).join('');
    }).catch(function(e){
      v.innerHTML = '<div class="error">block not found: ' + esc(e.message) + '</div>';
    });
  }

  function viewTx(id){
    var v = $('view');
    v.innerHTML = '<h1 class="page">Transaction</h1><p class="subline">loading…</p>';
    jget('/api/tx/' + id).then(function(d){
      var where = d.height >= 0
        ? 'confirmed in <a href="#/block/' + d.height + '">block #' + d.height + '</a>'
        : 'in mempool (not mined yet)';
      v.innerHTML = '<h1 class="page">Transaction</h1>'
        + '<p class="subline">' + where + '</p>'
        + txCard(d.tx, d.height);
    }).catch(function(){
      v.innerHTML = '<div class="error">transaction ' + esc(id) + ' not found on chain or in mempool</div>';
    });
  }

  // ── router ────────────────────────────────────────────────────────────
  function route(){
    var h = location.hash.replace(/^#\/?/, '');
    var parts = h.split('/');
    if (parts[0] === 'block' && parts[1] !== undefined && /^\d+$/.test(parts[1])){
      viewBlock(parseInt(parts[1], 10));
    } else if (parts[0] === 'tx' && parts[1]){
      viewTx(parts[1]);
    } else {
      viewHome();
    }
  }
  window.addEventListener('hashchange', route);

  // ── search ────────────────────────────────────────────────────────────
  $('search-form').addEventListener('submit', function(ev){
    ev.preventDefault();
    var q = $('search-input').value.trim().toLowerCase();
    if (!q) return;
    if (/^\d+$/.test(q)){
      location.hash = '#/block/' + parseInt(q, 10);
      return;
    }
    if (/^[0-9a-f]{64}$/.test(q)){
      // try tx first, then block hash
      jget('/api/tx/' + q).then(function(){
        location.hash = '#/tx/' + q;
      }).catch(function(){
        jget('/api/chain').then(function(c){
          var hit = (c.blocks || []).filter(function(b){ return b.hash === q; })[0];
          if (hit){ location.hash = '#/block/' + hit.height; }
          else { $('view').innerHTML = '<div class="error">no tx or block hash matches '
            + esc(q) + '</div>'; }
        });
      });
      return;
    }
    // prefix search over block hashes
    if (/^[0-9a-f]{6,63}$/.test(q)){
      jget('/api/chain').then(function(c){
        var hit = (c.blocks || []).filter(function(b){
          return b.hash.indexOf(q) === 0; })[0];
        if (hit){ location.hash = '#/block/' + hit.height; }
        else { $('view').innerHTML = '<div class="error">no block hash starts with '
          + esc(q) + '</div>'; }
      });
      return;
    }
    $('view').innerHTML = '<div class="error">search by block height, txid, or block hash prefix</div>';
  });

  refreshStats();
  setInterval(refreshStats, 5000);
  route();
})();