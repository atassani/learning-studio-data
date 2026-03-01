#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '..');
const dataDir = path.join(rootDir, 'data');

const strictTranslation = process.argv.includes('--strict-translation');
const minTranslatedRatio = 0.25;

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function collectQuestionTexts(payload) {
  if (!Array.isArray(payload.questions)) return [];
  return payload.questions.flatMap((q) => {
    const texts = [];
    if (typeof q.section === 'string') texts.push(q.section);
    if (typeof q.question === 'string') texts.push(q.question);
    if (Array.isArray(q.options)) {
      for (const option of q.options) {
        if (typeof option === 'string') texts.push(option);
      }
    }
    if (typeof q.answer === 'string') texts.push(q.answer);
    if (typeof q.explanation === 'string') texts.push(q.explanation);
    if (Array.isArray(q.appearsIn)) {
      for (const appears of q.appearsIn) {
        if (typeof appears === 'string') texts.push(appears);
      }
    }
    return texts;
  });
}

function compareTexts(source, target) {
  const total = Math.min(source.length, target.length);
  if (total === 0) return { total: 0, changed: 0, ratio: 0 };

  let changed = 0;
  for (let i = 0; i < total; i += 1) {
    if (source[i] !== target[i]) changed += 1;
  }
  return { total, changed, ratio: changed / total };
}

function formatPct(value) {
  return `${(value * 100).toFixed(1)}%`;
}

function main() {
  const areasPath = path.join(dataDir, 'areas.json');
  const areasPayload = readJson(areasPath);
  const areas = Array.isArray(areasPayload.areas) ? areasPayload.areas : [];

  const errors = [];
  const warnings = [];

  const entriesByShortName = new Map();
  const fileCache = new Map();

  for (const area of areas) {
    const file = area?.file;
    const shortName = area?.shortName;
    const language = area?.language;
    const type = area?.type;

    if (typeof file !== 'string' || typeof shortName !== 'string' || typeof language !== 'string') {
      errors.push(`Invalid area entry: ${JSON.stringify(area)}`);
      continue;
    }

    const filePath = path.join(dataDir, file);
    if (!fs.existsSync(filePath)) {
      errors.push(`Missing file for area ${shortName}/${language}: ${file}`);
      continue;
    }

    const payload = readJson(filePath);
    fileCache.set(file, payload);

    if (payload.language !== language) {
      errors.push(
        `Language mismatch in ${file}: areas.json has "${language}" but file has "${payload.language}"`
      );
    }
    if (payload.shortName !== shortName) {
      errors.push(
        `shortName mismatch in ${file}: areas.json has "${shortName}" but file has "${payload.shortName}"`
      );
    }
    if (typeof type === 'string' && payload.type !== type) {
      errors.push(`type mismatch in ${file}: areas.json has "${type}" but file has "${payload.type}"`);
    }

    const byLanguage = entriesByShortName.get(shortName) ?? new Map();
    byLanguage.set(language, { file, payload });
    entriesByShortName.set(shortName, byLanguage);
  }

  for (const [shortName, byLanguage] of entriesByShortName.entries()) {
    const es = byLanguage.get('es');
    const en = byLanguage.get('en');
    const ca = byLanguage.get('ca');

    if (!es) warnings.push(`Missing Spanish baseline for shortName "${shortName}"`);
    if (!en) warnings.push(`Missing English variant for shortName "${shortName}"`);
    if (!ca) warnings.push(`Missing Catalan variant for shortName "${shortName}"`);

    if (!es) continue;

    const sourceTexts = collectQuestionTexts(es.payload);
    for (const targetLang of ['en', 'ca']) {
      const target = byLanguage.get(targetLang);
      if (!target) continue;
      const targetTexts = collectQuestionTexts(target.payload);
      const comparison = compareTexts(sourceTexts, targetTexts);

      if (comparison.total === 0) {
        warnings.push(`No comparable question text for ${shortName} (${targetLang})`);
        continue;
      }

      const label = `${shortName} es->${targetLang}: ${comparison.changed}/${comparison.total} changed (${formatPct(comparison.ratio)})`;
      if (comparison.ratio < minTranslatedRatio) {
        warnings.push(`Low translation coverage, ${label}`);
      } else {
        console.log(`OK translation coverage, ${label}`);
      }
    }
  }

  if (warnings.length > 0) {
    console.log('\nWarnings:');
    for (const warning of warnings) {
      console.log(`- ${warning}`);
    }
  }

  if (errors.length > 0) {
    console.error('\nErrors:');
    for (const error of errors) {
      console.error(`- ${error}`);
    }
    process.exit(1);
  }

  if (strictTranslation && warnings.some((w) => w.startsWith('Low translation coverage'))) {
    process.exit(2);
  }
}

main();
