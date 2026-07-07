import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

// Force external links to open in a new tab
const defaultRender = md.renderer.rules.link_open || function(tokens, idx, options, env, self) {
  return self.renderToken(tokens, idx, options)
}
md.renderer.rules.link_open = function(tokens, idx, options, env, self) {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener')
  return defaultRender(tokens, idx, options, env, self)
}

export function renderMarkdown(text) {
  // Verified (node + jsdom harness): DOMPurify's default allowlist does NOT
  // include `target`, so without ADD_ATTR the sanitizer silently drops
  // target="_blank" from links, leaving them open in the same tab. `rel` is
  // already on the default allowlist and survives unmodified either way.
  return DOMPurify.sanitize(md.render(text), { ADD_ATTR: ['target'] })
}
