// Responsibility: Format and escape text for display.
// Owns: the escape every piece of backend text crosses before it is written into innerHTML.
// Boundaries: pure functions - no DOM, no network, no state.

/* Pure formatting and escaping. No DOM, no network, no state.
   `esc` is the boundary every piece of backend text crosses before it is written into
   innerHTML, so it is load-bearing for safety, not merely for looks. */
export function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function mdInline(t){return esc(t).replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/`(.+?)`/g,'<code>$1</code>');}
export function mdBlock(raw){if(!raw)return'';const lines=String(raw).split('\n');let out='',inUl=false,inTbl=false;
  for(let i=0;i<lines.length;i++){let l=lines[i];
    if(/^\|.+\|/.test(l)){if(!inTbl){out+='<table>';inTbl=true;}if(/^\|[-| :]+\|$/.test(l))continue;
      const tag=(i===0||/^\|[-| :]+\|$/.test(lines[i-1]||''))?'th':'td';
      out+='<tr>'+l.replace(/^\||\|$/g,'').split('|').map(c=>`<${tag}>${mdInline(c.trim())}</${tag}>`).join('')+'</tr>';continue;}
    else if(inTbl){out+='</table>';inTbl=false;}
    if(!/^[-*] /.test(l)&&inUl){out+='</ul>';inUl=false;}
    if(/^[-*] /.test(l)){if(!inUl){out+='<ul style="padding-left:18px;margin:4px 0">';inUl=true;}out+='<li>'+mdInline(l.slice(2))+'</li>';continue;}
    const h=l.match(/^(#{1,3}) (.+)/);if(h){out+=`<strong>${esc(h[2])}</strong><br>`;continue;}
    if(l.trim()===''){out+='<br>';continue;}out+=mdInline(l)+'<br>';}
  if(inTbl)out+='</table>';if(inUl)out+='</ul>';
  return out.replace(/(<br>){3,}/g,'<br><br>').replace(/<br>$/,'');}
export function fmtBytes(b){if(!b)return'-';if(b<1024)return b+' B';if(b<1048576)return(b/1024).toFixed(1)+' KB';return(b/1048576).toFixed(1)+' MB';}


export function fmtDur(ms){const t=Math.max(0,Math.round(ms/1000));
  const m=Math.floor(t/60),sec=t%60;return m+':'+String(sec).padStart(2,'0');}
