from __future__ import annotations

import json
import os


def build_page_reporter_js(port: int) -> str:
    return (
        "(function(){\n"
        "if(window.__bili_page_reporter__)return;\n"
        "window.__bili_page_reporter__=true;\n"
        "var last='';\n"
        "function sendPage(){\n"
        "  if(document.visibilityState!=='visible')return;\n"
        "  var url=window.location.href;\n"
        "  if(url.indexOf('live.bilibili.com')===-1)return;\n"
        "  var html=document.documentElement?document.documentElement.outerHTML:'';\n"
        "  var key=url+'|'+html.length;\n"
        "  if(key===last)return;\n"
        "  last=key;\n"
        "  fetch('http://127.0.0.1:"
        + str(port)
        + "/',{\n"
        "    method:'POST',\n"
        "    headers:{'Content-Type':'application/json'},\n"
        "    body:JSON.stringify({type:'__bili_page__',url:url,html:html})\n"
        "  }).catch(function(){});\n"
        "}\n"
        "sendPage();\n"
        "setInterval(sendPage,1000);\n"
        "document.addEventListener('visibilitychange',sendPage);\n"
        "window.addEventListener('load',sendPage);\n"
        "})();"
    )


def write_edge_extension(
    ext_dir: str,
    *,
    port: int,
    url_keyword: str | None,
    need_net: bool,
    need_cookie: bool,
    need_page: bool,
) -> None:
    manifest: dict = {
        "manifest_version": 3,
        "name": "BiliSniff",
        "version": "1.0",
        "host_permissions": ["http://127.0.0.1/*"],
        "content_scripts": [],
    }
    files: dict[str, str] = {}

    if need_net:
        manifest["content_scripts"] += [
            {
                "matches": ["*://*.bilibili.com/*"],
                "js": ["inject.js"],
                "run_at": "document_start",
                "world": "MAIN",
            },
            {
                "matches": ["*://*.bilibili.com/*"],
                "js": ["relay.js"],
                "run_at": "document_start",
            },
        ]
        files["inject.js"] = (
            "(function(){\n"
            "var origFetch=window.fetch;\n"
            "window.fetch=async function(){\n"
            "  var resp=await origFetch.apply(this,arguments);\n"
            "  var url=(typeof arguments[0]==='string')?arguments[0]:arguments[0].url;\n"
            "  if(document.visibilityState==='visible'&&url.indexOf('"
            + (url_keyword or "")
            + "')!==-1){\n"
            "    try{var d=await resp.clone().json();\n"
            "      window.postMessage({type:'__bili_sniff__',payload:{url:url,data:d,page_url:window.location.href}},'*');\n"
            "    }catch(e){}\n"
            "  }\n"
            "  return resp;\n"
            "};\n"
            "var origOpen=XMLHttpRequest.prototype.open;\n"
            "var origSend=XMLHttpRequest.prototype.send;\n"
            "XMLHttpRequest.prototype.open=function(m,u){\n"
            "  this.__url=u;return origOpen.apply(this,arguments);};\n"
            "XMLHttpRequest.prototype.send=function(){\n"
            "  var self=this;\n"
            "  this.addEventListener('load',function(){\n"
            "    if(document.visibilityState==='visible'&&self.__url&&self.__url.indexOf('"
            + (url_keyword or "")
            + "')!==-1){\n"
            "      try{window.postMessage({type:'__bili_sniff__',\n"
            "        payload:{url:self.__url,data:JSON.parse(self.responseText),page_url:window.location.href}},'*');\n"
            "      }catch(e){}\n"
            "    }\n"
            "  });\n"
            "  return origSend.apply(this,arguments);\n"
            "};\n"
            "})();"
        )
        files["relay.js"] = (
            "window.addEventListener('message',function(e){\n"
            "  if(e.data&&e.data.type==='__bili_sniff__'){\n"
            "    fetch('http://127.0.0.1:"
            + str(port)
            + "/',{\n"
            "      method:'POST',\n"
            "      headers:{'Content-Type':'application/json'},\n"
            "      body:JSON.stringify(e.data.payload)\n"
            "    }).catch(function(){});\n"
            "  }\n"
            "});"
        )

    if need_page:
        manifest["content_scripts"].append(
            {
                "matches": ["*://*.bilibili.com/*"],
                "js": ["page.js"],
                "run_at": "document_idle",
            }
        )
        files["page.js"] = build_page_reporter_js(port)

    if need_cookie:
        manifest["permissions"] = ["cookies"]
        manifest["host_permissions"].append("*://*.bilibili.com/*")
        manifest["background"] = {"service_worker": "background.js"}
        files["background.js"] = (
            "function checkCookies(){\n"
            "  chrome.cookies.getAll({domain:'.bilibili.com'},function(cookies){\n"
            "    if(!cookies.some(function(c){return c.name==='SESSDATA';}))return;\n"
            "    fetch('http://127.0.0.1:"
            + str(port)
            + "/',{\n"
            "      method:'POST',\n"
            "      headers:{'Content-Type':'application/json'},\n"
            "      body:JSON.stringify({type:'__bili_cookies__',cookies:cookies})\n"
            "    }).catch(function(){});\n"
            "  });\n"
            "}\n"
            "checkCookies();\n"
            "setInterval(checkCookies,3000);"
        )

    with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    for fname, content in files.items():
        with open(os.path.join(ext_dir, fname), "w", encoding="utf-8") as f:
            f.write(content)


def write_chrome_extension(
    ext_dir: str,
    *,
    port: int,
    url_keyword: str | None,
    need_net: bool,
    need_cookie: bool,
    need_page: bool,
) -> None:
    relay_js = (
        "window.addEventListener('message',function(e){\n"
        "  if(e.data && e.data.type==='__bili_sniff__'){\n"
        "    fetch('http://127.0.0.1:"
        + str(port)
        + "/',{\n"
        "      method:'POST',\n"
        "      headers:{'Content-Type':'application/json'},\n"
        "      body:JSON.stringify(e.data.payload)\n"
        "    }).catch(function(){});\n"
        "  }\n"
        "});"
    )

    background_js = (
        "var NEED_NET = "
        + ("true" if need_net else "false")
        + ";\n"
        "var NEED_COOKIE = "
        + ("true" if need_cookie else "false")
        + ";\n"
        "var injectedTabs = {};\n"
        "var PORT = "
        + str(port)
        + ";\n"
        "function injectTab(tabId) {\n"
        "  if (!NEED_NET || injectedTabs[tabId]) return;\n"
        "  injectedTabs[tabId] = true;\n"
        "  chrome.scripting.executeScript({\n"
        "    target: {tabId: tabId},\n"
        "    world: 'ISOLATED',\n"
        "    func: function() {\n"
        "      window.addEventListener('message', function(e) {\n"
        "        if (e.data && e.data.type === '__bili_sniff__') {\n"
        "          fetch('http://127.0.0.1:"
        + str(port)
        + "/', {\n"
        "            method: 'POST',\n"
        "            headers: {'Content-Type': 'application/json'},\n"
        "            body: JSON.stringify(e.data.payload)\n"
        "          }).catch(function(){});\n"
        "        }\n"
        "      });\n"
        "    }\n"
        "  });\n"
        "  chrome.scripting.executeScript({\n"
        "    target: {tabId: tabId},\n"
        "    world: 'MAIN',\n"
        "    func: function() {\n"
        "      if (window.__bili_sniff_injected__) return;\n"
        "      window.__bili_sniff_injected__ = true;\n"
        "      var kw = '"
        + (url_keyword or "")
        + "';\n"
        "      var origFetch = window.fetch;\n"
        "      window.fetch = async function() {\n"
        "        var resp = await origFetch.apply(this, arguments);\n"
        "        var url = (typeof arguments[0] === 'string') ? arguments[0] : arguments[0].url;\n"
        "        if (document.visibilityState === 'visible' && url.indexOf(kw) !== -1) {\n"
        "          try {\n"
        "            var d = await resp.clone().json();\n"
        "            window.postMessage({type: '__bili_sniff__', payload: {url: url, data: d, page_url: window.location.href}}, '*');\n"
        "          } catch(e) {}\n"
        "        }\n"
        "        return resp;\n"
        "      };\n"
        "      var origOpen = XMLHttpRequest.prototype.open;\n"
        "      var origSend = XMLHttpRequest.prototype.send;\n"
        "      XMLHttpRequest.prototype.open = function(m, u) {\n"
        "        this.__url = u; return origOpen.apply(this, arguments);\n"
        "      };\n"
        "      XMLHttpRequest.prototype.send = function() {\n"
        "        var self = this;\n"
        "        this.addEventListener('load', function() {\n"
        "          if (document.visibilityState === 'visible' && self.__url && self.__url.indexOf(kw) !== -1) {\n"
        "            try {\n"
        "              window.postMessage({type: '__bili_sniff__', payload: {url: self.__url, data: JSON.parse(self.responseText), page_url: window.location.href}}, '*');\n"
        "            } catch(e) {}\n"
        "          }\n"
        "        });\n"
        "        return origSend.apply(this, arguments);\n"
        "      };\n"
        "    }\n"
        "  });\n"
        "}\n"
        "if (NEED_NET) {\n"
        "chrome.tabs.onUpdated.addListener(function(tabId, changeInfo, tab) {\n"
        "  if (changeInfo.status === 'loading') {\n"
        "    delete injectedTabs[tabId];\n"
        "    return;\n"
        "  }\n"
        "  if (changeInfo.status === 'complete' && tab.url && tab.url.indexOf('bilibili.com') !== -1) {\n"
        "    injectTab(tabId);\n"
        "  }\n"
        "});\n"
        "chrome.tabs.query({url: '*://*.bilibili.com/*'}, function(tabs) {\n"
        "  tabs.forEach(function(tab) { injectTab(tab.id); });\n"
        "});\n"
        "}\n"
        "function sendCookies() {\n"
        "  if (!NEED_COOKIE) return;\n"
        "  chrome.cookies.getAll({domain: '.bilibili.com'}, function(cookies) {\n"
        "    if (cookies && cookies.length > 0) {\n"
        "      fetch('http://127.0.0.1:' + PORT + '/', {\n"
        "        method: 'POST',\n"
        "        headers: {'Content-Type': 'application/json'},\n"
        "        body: JSON.stringify({type: '__bili_cookies__', cookies: cookies})\n"
        "      }).catch(function(){});\n"
        "    }\n"
        "  });\n"
        "}\n"
        "if (NEED_COOKIE) {\n"
        "sendCookies();\n"
        "setInterval(sendCookies, 3000);\n"
        "chrome.cookies.onChanged.addListener(function(changeInfo) {\n"
        "  if (changeInfo.cookie.domain.includes('bilibili.com')) {\n"
        "    sendCookies();\n"
        "  }\n"
        "});\n"
        "}\n"
    )

    manifest = {
        "manifest_version": 3,
        "name": "BiliSniff",
        "version": "1.0",
        "permissions": ["scripting", "tabs", "cookies"],
        "host_permissions": [
            "http://127.0.0.1/*",
            "*://*.bilibili.com/*",
        ],
        "background": {"service_worker": "background.js"},
        "content_scripts": [],
    }

    if need_net:
        manifest["content_scripts"].append(
            {
                "matches": ["*://*.bilibili.com/*"],
                "js": ["relay.js"],
                "run_at": "document_start",
            }
        )

    if need_page:
        manifest["content_scripts"].append(
            {
                "matches": ["*://*.bilibili.com/*"],
                "js": ["page.js"],
                "run_at": "document_idle",
            }
        )

    ext_path = os.path.join(ext_dir, "manifest.json")
    with open(ext_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
        f.write(background_js)

    if need_net:
        with open(os.path.join(ext_dir, "relay.js"), "w", encoding="utf-8") as f:
            f.write(relay_js)

    if need_page:
        with open(os.path.join(ext_dir, "page.js"), "w", encoding="utf-8") as f:
            f.write(build_page_reporter_js(port))
