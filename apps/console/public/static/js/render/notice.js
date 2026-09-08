// Responsibility: Tell the user something went wrong, in a way they can act on.
// Boundaries: it renders the notice region only - it registers no window listener and wraps no fetch.

/* User-facing notices: offline, expired session, server trouble.
 *
 * One place to tell the user something went wrong in a way they can act on - instead of a
 * silent `return`, a raw error string in the chat, or a browser alert(). role="status" +
 * aria-live so a screen reader announces it.
 *
 * This file used to also contain the shared fetch wrapper and register window-level listeners
 * at import time. The wrapper is now the HTTP boundary (api/client.js) and the listeners are
 * wired by the entrypoint, so importing a renderer no longer attaches global handlers.
 */
export const Notice = {
  el(){return document.getElementById('notice');},
  _sticky:{},
  show(msg,kind,opts){
    opts=opts||{};const n=this.el();if(!n)return;
    n.innerHTML=`<span class="nt-msg">${(msg||'').replace(/</g,'&lt;')}</span>`
      +(opts.action?`<button class="nt-act" id="nt-act">${opts.action}</button>`:'')
      +`<button class="nt-x" aria-label="Dismiss">×</button>`;
    n.className='show '+(kind||'info');
    n.querySelector('.nt-x').onclick=()=>this.clear();
    if(opts.action&&opts.onAction){n.querySelector('#nt-act').onclick=opts.onAction;}
    clearTimeout(this._t);
    if(!opts.sticky)this._t=setTimeout(()=>this.clear(),opts.ms||7000);
  },
  clear(){const n=this.el();if(n){n.className='';n.innerHTML='';}},
  // a persistent condition (offline, expired session) that must stay until resolved
  hold(key,msg,kind,opts){this._sticky[key]=1;this.show(msg,kind,Object.assign({sticky:true},opts||{}));},
  release(key){delete this._sticky[key];if(!Object.keys(this._sticky).length)this.clear();},
};
