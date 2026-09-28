// Sector holders: "human:<username>" or "ai:<agent>". One colour per holder, used by the 3D
// sectorization, the traffic symbols, data blocks and the sectors list.

export const MINE = '#5cc8ff';
export const AI = '#d08cff';
export const FREE = '#5f7b99';
const OTHERS = ['#ff9f6b', '#7be0c3', '#f5d76e', '#9fb5ff', '#ff8fb8', '#b4e36b', '#ffc38a', '#8fd3ff'];

export function holderColor(holder, me) {
  if (!holder) return FREE;
  if (holder === me) return MINE;
  if (holder.startsWith('ai:')) return AI;
  let h = 0;
  for (const c of holder) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return OTHERS[h % OTHERS.length];
}

/** "human:alice" -> "alice", "ai:rules" -> "AI (rules)"; the viewer sees "you". */
export function holderName(holder, me) {
  if (!holder) return 'free';
  if (holder === me) return 'you';
  const [kind, name] = [holder.slice(0, holder.indexOf(':')), holder.slice(holder.indexOf(':') + 1)];
  return kind === 'ai' ? `AI (${name})` : name;
}
