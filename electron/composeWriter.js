/**
 * composeWriter.js
 *
 * Generates docker-compose.runtime.yml with dynamically chosen host ports.
 * Internal container ports stay fixed — only the host-side mappings change.
 * This file is never committed to git.
 */

'use strict'

const fs   = require('fs')
const path = require('path')

/**
 * Write docker-compose.runtime.yml next to the static docker-compose.yml.
 *
 * @param {string} composeDir  Directory where compose files live
 * @param {{ postgres: number, neo4jBolt: number, neo4jBrowser: number, qdrant: number }} ports
 * @returns {string}  Full path to the written file
 */
function writeRuntimeCompose(composeDir, ports) {
  const content = `# AUTO-GENERATED — do not edit. Regenerated on every launch.
# Source: electron/composeWriter.js
services:
  postgres:
    image: pgvector/pgvector:pg16
    container_name: codelm_postgres
    restart: unless-stopped
    environment:
      POSTGRES_DB: codelm
      POSTGRES_USER: codelm
      POSTGRES_PASSWORD: codelm
    ports:
      - "${ports.postgres}:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data

  neo4j:
    image: neo4j:5.20-community
    container_name: codelm_neo4j
    restart: unless-stopped
    environment:
      NEO4J_AUTH: neo4j/codelm
      NEO4J_PLUGINS: '["apoc"]'
    ports:
      - "${ports.neo4jBrowser}:7474"
      - "${ports.neo4jBolt}:7687"
    volumes:
      - neo4j_data:/data

  qdrant:
    image: qdrant/qdrant:v1.13.3
    container_name: codelm_qdrant
    restart: unless-stopped
    ports:
      - "${ports.qdrant}:6333"
    volumes:
      - qdrant_data:/qdrant/storage

volumes:
  postgres_data:
  neo4j_data:
  qdrant_data:
`
  const outPath = path.join(composeDir, 'docker-compose.runtime.yml')
  fs.writeFileSync(outPath, content, 'utf8')
  return outPath
}

module.exports = { writeRuntimeCompose }
