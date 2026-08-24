import { describe, expect, it } from 'vitest';
import styles from '../styles.css?raw';

describe('source discovery responsive layout', () => {
  it('allows a discovery record to shrink so its table scrolls inside the card', () => {
    expect(styles).toMatch(/\.discovery-record\{[^}]*min-width:0/);
  });
});
