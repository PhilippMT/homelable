/**
 * What a document can embed, and the markdown an upload becomes.
 *
 * The list mirrors `ALLOWED_TYPES` in the backend's `api/routes/media.py`,
 * which stays the authority: checking here only spares a round trip and lets
 * the refusal name the formats instead of relaying a 415.
 */

export const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp']

/** The same list, as a sentence can carry it. */
export const SUPPORTED_LABEL = 'PNG, JPEG or WebP'

export function isSupportedMedia(file: File): boolean {
  return IMAGE_TYPES.includes(file.type)
}

/**
 * The markdown that shows an uploaded file.
 *
 * The alt text is the file name without its extension. Brackets are dropped
 * rather than escaped: they would close the alt early, and the source is meant
 * to stay readable.
 */
export function mediaMarkdown(file: File, url: string): string {
  const alt = file.name.replace(/\.[^.]+$/, '').replace(/[[\]\s]+/g, ' ').trim() || 'image'
  return `![${alt}](${url})`
}

/** The server's own reason when it gave one — it names the limit that was hit. */
export function uploadError(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof detail === 'string' ? detail : 'Upload failed'
}
